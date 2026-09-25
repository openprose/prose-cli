import { afterEach, describe, expect, test } from "bun:test";
import { chmod, lstat, mkdir, mkdtemp, readFile, readdir, realpath, rm, writeFile } from "node:fs/promises";
import { delimiter, dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import oracle from "../../shared/capabilities/adapters/oracle.v1.json" with { type: "json" };
import claudeScenario from "../../shared/fixtures/adapters/scenarios/claude-print-stream-json.v1.json" with { type: "json" };
import codexScenario from "../../shared/fixtures/adapters/scenarios/codex-exec-json.v1.json" with { type: "json" };
import ompScenario from "../../shared/fixtures/adapters/scenarios/omp-rpc.v1.json" with { type: "json" };
import primeScenario from "../../shared/fixtures/adapters/scenarios/prime-rpc.v1.json" with { type: "json" };
import taskFixture from "../../shared/fixtures/transport/sentinel-task.json" with { type: "json" };
import terminalCarriage from "../../shared/fixtures/adapters/functional-alpha/terminal-carriage.v1.json" with { type: "json" };
import { buildInstalledAdapterEnvironment, installedAdapterProtectedValues } from "../src/adapters/environment";
import { assertInstalledAdapterArgv, assertInstalledAdapterPlatform } from "../src/adapters/admission";
import {
  assertInstalledAdapterRuntimePrerequisites,
  inspectInstalledAdapterRuntimePrerequisites,
  probeInstalledAdapterAuth,
  probeInstalledAdapterVersion,
  resolveInstalledExecutable,
} from "../src/adapters/executable";
import { buildInstalledLaunchPlan } from "../src/adapters/plan";
import { installedAdapterDefinition, installedAdapterIds as allInstalledAdapterIds } from "../src/adapters/recipes";
import { runProviderFreeInstalledAdapter } from "../src/adapters/runner";
import { imageTerminalSchema, recoverImageTerminalEnvelope } from "../src/adapters/terminal";
import type { InstalledAdapterId } from "../src/adapters/types";
import { sentinelImage } from "../src/assets/sentinel";
import { runCli, type CliDependencies } from "../src/cli";
import { canonicalJson, sha256, verifyRuntimeImage } from "../src/core/image";
import { formatHumanError } from "../src/core/output";
import { RunnerFailure, type RunnerInvocation, type TaskEnvelope } from "../src/core/types";
import { encodeRuntimeImage, OMP_CONTROL_OVERLAY_BYTES } from "../src/supervision/files";
import { sentinelFixtureImage } from "./sentinel-fixture";

const probe = resolve(import.meta.dir, "../../shared/fixtures/adapters/bin/adapter_probe.py");
const python = new TextDecoder().decode(Bun.spawnSync({
  cmd: ["python3", "-c", "import sys; print(sys.executable)"],
  stdout: "pipe",
  stderr: "pipe",
}).stdout).trim();
const installedAdapterIds = allInstalledAdapterIds.filter(id => id !== "agents-sdk/jsonl");
const adapterCases: InstalledAdapterId[] = [...installedAdapterIds];
const scenarioValues = [codexScenario, claudeScenario, primeScenario, ompScenario];
// This suite replays the four historical scenario fixtures; SDK has a dedicated native suite.
const scenarios = new Map(scenarioValues.map((scenario) => [scenario.adapterId as InstalledAdapterId, scenario]));
const roots: string[] = [];
const liveVersions: Record<InstalledAdapterId, string> = {
  "agents-sdk/jsonl":"prose-agents-sdk 0.1.0",
    "codex/exec-json": "codex-cli 0.149.0-alpha.4.1",
  "claude/print-stream-json": "2.1.243 (Claude Code)",
  "prime/rpc": "prime-agent 0.7.0",
  "omp/rpc": "omp/18.0.9",
};
// These ceilings bound test orchestration only. The production 5 s deadline
// still applies independently to every version or runtime probe below.
const SERIAL_RUNTIME_CLEANUP_TEST_TIMEOUT_MS = 30_000;
const SERIAL_PATH_AUTHORITY_TEST_TIMEOUT_MS = 15_000;

afterEach(async () => {
  await Promise.all(roots.splice(0).map((root) => rm(root, { recursive: true, force: true })));
});

async function fixture(): Promise<{
  root: string;
  invocation: RunnerInvocation;
  image: Awaited<ReturnType<typeof verifyRuntimeImage>>;
}> {
  const root = await mkdtemp(join(tmpdir(), "openprose-bun-adapter-"));
  roots.push(root);
  const requestedWorkspace = join(root, "workspace with spaces 雪");
  await Bun.write(join(requestedWorkspace, ".keep"), "");
  const workspace = await realpath(requestedWorkspace);
  const image = await verifyRuntimeImage(sentinelImage);
  const task = taskFixture as TaskEnvelope;
  const invocation: RunnerInvocation = {
    schema: "openprose.runner-invocation/1",
    invocationId: "fixture-invocation-0001",
    cwd: workspace,
    languageImage: {
      formatVersion: image.manifest.imageFormatVersion,
      version: image.manifest.imageVersion,
      sha256: image.aggregateSha256,
    },
    runner: { name: "bun", version: "0.1.0", commit: "test" },
    harness: "fixture",
    transport: "fixture",
    recursionToken: "openprose:fixture-invocation-0001",
    task,
    taskDigestSha256: await sha256(canonicalJson(task)),
  };
  return { root, invocation, image };
}

function credentialGroup(adapterId: InstalledAdapterId): string {
  return scenarios.get(adapterId)!.environment.credentialGroup;
}

function ambientFor(adapterId: InstalledAdapterId): Record<string, string> {
  const definition = installedAdapterDefinition(adapterId);
  const selected = credentialGroup(adapterId);
  const ambient: Record<string, string> = {
    PATH: process.env.PATH ?? dirname(process.execPath),
    HOME: "/fixture/home",
    USER: "fixture-posix-user",
    USERPROFILE: "C:\\fixture\\profile",
    LANG: "C.UTF-8",
    __CF_USER_TEXT_ENCODING: "fixture:__CF_USER_TEXT_ENCODING",
    RANDOM_AMBIENT: "must-not-pass",
    OPENPROSE_TOKEN: "openprose-secret-must-not-pass",
    PROSE_TOKEN: "legacy-secret-must-not-pass",
    PRIME_AGENT_CODING_AGENT_DIR: "/fixture/prime-store-override",
    PI_CODING_AGENT_DIR: "/fixture/omp-store-override",
    PI_CONFIG_FILES: "/fixture/hostile-omp-config.yml",
    PRIME_AGENT_TELEMETRY: "1",
  };
  for (const [group, names] of Object.entries(definition.credentialGroups)) {
    for (const name of names) ambient[name] = `fixture-secret:${group}:${name}`;
  }
  return ambient;
}

async function writeLiveHarness(
  root: string,
  adapterId: InstalledAdapterId,
  assistantText: string,
  observationPath: string,
  authReady = true,
  terminalDelayMs = 0,
  versionObservationPath: string | null = null,
  stderrText: string | null = null,
): Promise<string> {
  if (adapterId === "omp/rpc") await writeBunRuntime(root, "1.3.14");
  const name = installedAdapterDefinition(adapterId).recipe.identity.executableNames[0]!;
  const executable = join(root, name);
  const source = [
    `#!${process.execPath}`,
    "(async () => {",
    "const argv = process.argv.slice(2);",
    `const adapterId = ${JSON.stringify(adapterId)};`,
    `const version = ${JSON.stringify(liveVersions[adapterId])};`,
    `let assistantText = ${JSON.stringify(assistantText)};`,
    `const observationPath = ${JSON.stringify(observationPath)};`,
    `const versionObservationPath = ${JSON.stringify(versionObservationPath)};`,
    `let stderrText = ${JSON.stringify(stderrText)};`,
    "const credentialConfigName = adapterId === 'prime/rpc' ? 'PRIME_AGENT_CODING_AGENT_DIR' : adapterId === 'omp/rpc' ? 'PI_CODING_AGENT_DIR' : undefined;",
    "const credentialConfigPath = credentialConfigName === undefined ? undefined : process.env[credentialConfigName];",
    "assistantText = assistantText.replaceAll('{{OPENPROSE_TEST_CREDENTIAL_CONFIG_PATH}}', credentialConfigPath ?? '');",
    "if (stderrText !== null) stderrText = stderrText.replaceAll('{{OPENPROSE_TEST_CREDENTIAL_CONFIG_PATH}}', credentialConfigPath ?? '');",
    "const credentialConfig = credentialConfigPath === undefined ? null : {name:credentialConfigName,path:credentialConfigPath,existsAtHarnessStart:require('node:fs').existsSync(credentialConfigPath),mode:require('node:fs').statSync(credentialConfigPath).mode & 0o777};",
    "const adapterControls = adapterId === 'prime/rpc' ? {PRIME_AGENT_TELEMETRY:process.env.PRIME_AGENT_TELEMETRY} : {};",
    "const configIndex=argv.lastIndexOf('--config'); const configPath=configIndex < 0 ? undefined : argv[configIndex+1]; const configFile=configPath===undefined ? null : {path:configPath,existsAtHarnessStart:require('node:fs').existsSync(configPath),mode:require('node:fs').statSync(configPath).mode & 0o777,bytes:require('node:fs').readFileSync(configPath,'utf8')};",
    "const observation = () => ({argv,stdin,environmentNames:Object.keys(process.env).sort(),credentialConfig,adapterControls,configFile});",
    `const terminalDelayMs = ${terminalDelayMs};`,
    "if (argv.includes('--version') || argv.includes('-v')) { if (versionObservationPath !== null) await Bun.write(versionObservationPath, JSON.stringify(Object.keys(process.env).sort())); (adapterId === 'prime/rpc' ? console.error : console.log)(version); return; }",
    `const authReady = ${String(authReady)};`,
    "if (adapterId === 'codex/exec-json' && argv.join(' ') === 'login status') { console.log(authReady ? 'Logged in using ChatGPT' : 'Not logged in'); return; }",
    "if (adapterId === 'claude/print-stream-json' && argv.join(' ') === 'auth status --json') { console.log(JSON.stringify({loggedIn:authReady})); return; }",
    "if (adapterId === 'prime/rpc' && argv.join(' ') === 'model list') { await Bun.write(observationPath+'.auth-probe','called'); console.error('provider model'); return; }",
    "if (adapterId === 'omp/rpc' && argv.join(' ') === '--help') { await Bun.write(observationPath+'.auth-probe','called'); console.log('Usage: omp'); return; }",
    "if (stderrText !== null) console.error(stderrText);",
    "const rpc = adapterId === 'prime/rpc' || adapterId === 'omp/rpc';",
    "let stdin = ''; let trailing = ''; const iterator = process.stdin[Symbol.asyncIterator]();",
    "if (adapterId === 'omp/rpc') {",
    " console.log(JSON.stringify({type:'ready',protocolVersion:1,supportedProtocolVersions:[1,2],maxFrameBytes:1048576,maxReassembledFrameBytes:67108864}));",
    " console.log(JSON.stringify({type:'available_commands_update',commands:[]}));",
    " while (!stdin.includes('\\n')) { const next = await iterator.next(); if (next.done) { console.error('early state EOF'); process.exitCode=91; return; } stdin += String(next.value); }",
    " let split=stdin.indexOf('\\n')+1; trailing=stdin.slice(split); stdin=stdin.slice(0,split); const state=JSON.parse(stdin.trim());",
    " if (state.type!=='get_state' || typeof state.id!=='string' || Object.keys(state).sort().join(',')!=='id,type') { console.error('unexpected state request'); process.exitCode=93; return; }",
    " console.log(JSON.stringify({id:state.id,type:'response',command:'get_state',success:true,data:{dumpTools:[]}}));",
    " while (!trailing.includes('\\n')) { const next = await iterator.next(); if (next.done) { console.error('early prompt EOF'); process.exitCode=91; return; } trailing += String(next.value); }",
    " split=trailing.indexOf('\\n')+1; stdin += trailing.slice(0,split); trailing=trailing.slice(split);",
    " await Bun.sleep(10); if (process.stdin.readableEnded) { console.error('early stdin EOF'); process.exitCode=91; return; }",
    "} else if (rpc) {",
    " while (!stdin.includes('\\n')) { const next = await iterator.next(); if (next.done) { console.error('early stdin EOF'); process.exitCode=91; return; } stdin += String(next.value); }",
    " const split = stdin.indexOf('\\n') + 1; trailing = stdin.slice(split); stdin = stdin.slice(0, split);",
    " await Bun.sleep(10); if (process.stdin.readableEnded) { console.error('early stdin EOF'); process.exitCode=91; return; }",
    "} else { for await (const chunk of process.stdin) stdin += chunk; }",
    "if (adapterId === 'codex/exec-json') {",
    " console.log(JSON.stringify({type:'thread.started',thread_id:'fixture-thread'}));",
    " console.log(JSON.stringify({type:'turn.started'}));",
    " console.log(JSON.stringify({type:'item.completed',item:{type:'agent_message',text:assistantText}})); await Bun.sleep(terminalDelayMs);",
    " console.log(JSON.stringify({type:'turn.completed'})); await Bun.write(observationPath, JSON.stringify(observation())); return;",
    "}",
    "if (adapterId === 'claude/print-stream-json') {",
    " const session_id='fixture-session';",
    " console.log(JSON.stringify({type:'system',subtype:'init',session_id}));",
    " console.log(JSON.stringify({type:'assistant',session_id,message:{content:[{type:'text',text:assistantText}]}})); await Bun.sleep(terminalDelayMs);",
    " console.log(JSON.stringify({type:'result',subtype:'success',is_error:false,session_id})); await Bun.write(observationPath, JSON.stringify(observation())); return;",
    "}",
    "const prompt = JSON.parse(stdin.trim().split('\\n').at(-1));",
    "if (adapterId === 'prime/rpc') {",
    " const user={role:'user',content:[{type:'text',text:prompt.message}],timestamp:1700000000000};",
    " const usage={input:0,output:0,cacheRead:0,cacheWrite:0,totalTokens:0,cost:{input:0,output:0,cacheRead:0,cacheWrite:0,total:0}};",
    " const assistant=(content,responseId=false)=>({role:'assistant',content,api:'fixture-jsonl',provider:'fixture',model:'fixture/model',usage,stopReason:'stop',timestamp:1700000000001,...(responseId?{responseId:'fixture-response-id'}:{})});",
    " const thinkingText='synthetic reasoning summary'; const thinking={type:'thinking',thinking:thinkingText,thinkingSignature:'fixture-thinking-signature'}; const thinkingStarted=assistant([{type:'thinking',thinking:''}],true); const thinkingEnded=assistant([thinking],true); const textStarted=assistant([thinking,{type:'text',text:''}],true); const textComplete=assistant([thinking,{type:'text',text:assistantText,textSignature:'fixture-text-signature'}],true);",
    " console.log(JSON.stringify({id:prompt.id,type:'response',command:'prompt',success:true}));",
    " console.log(JSON.stringify({type:'agent_start'})); console.log(JSON.stringify({type:'turn_start'}));",
    " console.log(JSON.stringify({type:'message_start',message:user})); console.log(JSON.stringify({type:'message_end',message:user}));",
    " console.log(JSON.stringify({type:'message_start',message:assistant([])}));",
    " console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'thinking_start',contentIndex:0},message:thinkingStarted}));",
    " let streamedThinking=''; const thinkingFirst=Math.floor(thinkingText.length/3); const thinkingSecond=Math.floor(thinkingText.length*2/3); for (const delta of [thinkingText.slice(0,thinkingFirst),thinkingText.slice(thinkingFirst,thinkingSecond),thinkingText.slice(thinkingSecond)+'\\n\\n']) { streamedThinking+=delta; console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'thinking_delta',contentIndex:0,delta},message:assistant([{type:'thinking',thinking:streamedThinking}],true)})); }",
    " console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'thinking_end',contentIndex:0,content:thinkingText},message:thinkingEnded}));",
    " console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'text_start',contentIndex:1},message:textStarted}));",
    " let streamed=''; const first=Math.floor(assistantText.length/3); const second=Math.floor(assistantText.length*2/3); for (const delta of [assistantText.slice(0,first),assistantText.slice(first,second),assistantText.slice(second)]) { streamed+=delta; console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'text_delta',contentIndex:1,delta},message:assistant([thinking,{type:'text',text:streamed}],true)})); }",
    " console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'text_end',contentIndex:1,content:assistantText},message:textComplete}));",
    " console.log(JSON.stringify({type:'message_end',message:textComplete})); await Bun.sleep(terminalDelayMs); console.log(JSON.stringify({type:'turn_end',message:textComplete,toolResults:[]}));",
    " console.log(JSON.stringify({type:'agent_end',messages:[user,textComplete]}));",
    "} else {",
    " const user={role:'user',content:{}}; const assistant={role:'assistant',content:[{type:'text',text:assistantText}]};",
    " console.log(JSON.stringify({type:'agent_start'})); console.log(JSON.stringify({type:'turn_start'}));",
    " console.log(JSON.stringify({type:'message_start',message:user})); console.log(JSON.stringify({type:'message_end',message:user}));",
    " console.log(JSON.stringify({type:'message_start',message:{role:'assistant',content:[]}}));",
    " console.log(JSON.stringify({type:'message_update',assistantMessageEvent:{type:'text_delta',delta:assistantText},message:{role:'assistant',content:[]}}));",
    " console.log(JSON.stringify({type:'message_end',message:assistant})); await Bun.sleep(terminalDelayMs); console.log(JSON.stringify({type:'turn_end',message:assistant,toolResults:[]}));",
    " console.log(JSON.stringify({type:'agent_end',messages:[user,assistant],isTerminal:true}));",
    "}",
    "while (true) { const next=await iterator.next(); if (next.done) break; trailing += String(next.value); }",
    "if (trailing.length !== 0) { console.error('unexpected bytes after prompt'); process.exitCode=92; return; }",
    "if (adapterId === 'omp/rpc') console.log(JSON.stringify({id:prompt.id,type:'response',command:'prompt',success:true}));",
    "await Bun.write(observationPath, JSON.stringify(observation()));",
    "})().catch((error) => { console.error(error); process.exitCode = 1; });",
  ].join("\n");
  await writeFile(executable, source, { mode: 0o700 });
  await chmod(executable, 0o700);
  return executable;
}

async function writeBunRuntime(
  directory: string,
  output: string,
  observationPath: string | null = null,
): Promise<string> {
  await mkdir(directory, { recursive: true });
  const executable = join(directory, "bun");
  const source = [
    `#!${process.execPath}`,
    ...(observationPath === null
      ? []
      : [
          `await Bun.write(${JSON.stringify(observationPath)}, JSON.stringify({argv:process.argv.slice(2),environmentNames:Object.keys(process.env).sort()}));`,
        ]),
    `console.log(${JSON.stringify(output)});`,
  ].join("\n");
  await writeFile(executable, `${source}\n`, { mode: 0o700 });
  await chmod(executable, 0o700);
  return executable;
}

async function writePreflightVersionExecutable(
  executable: string,
  version: string,
  stream: "stdout" | "stderr",
  unexpectedLaunchPath: string,
): Promise<void> {
  const emit = stream === "stdout" ? "sys.stdout" : "sys.stderr";
  const source = [
    `#!${python}`,
    "import json, os, sys",
    "argv = sys.argv[1:]",
    "if argv == ['--version'] or argv == ['-v']:",
    `    ${emit}.write(${JSON.stringify(`${version}\n`)})`,
    `    ${emit}.flush()`,
    "    raise SystemExit(0)",
    `with open(${JSON.stringify(unexpectedLaunchPath)}, 'x', encoding='utf-8') as target:`,
    "    json.dump({'argv': argv}, target)",
    "raise SystemExit(97)",
  ].join("\n");
  await writeFile(executable, `${source}\n`, { mode: 0o700 });
  await chmod(executable, 0o700);
}

async function writePreflightOnlyHarness(
  root: string,
  adapterId: "prime/rpc" | "omp/rpc",
  unexpectedLaunchPath: string,
): Promise<void> {
  if (adapterId === "omp/rpc") {
    await writePreflightVersionExecutable(
      join(root, "bun"),
      "1.3.14",
      "stdout",
      `${unexpectedLaunchPath}.runtime-launch`,
    );
  }
  const executableName = installedAdapterDefinition(adapterId).recipe.identity.executableNames[0]!;
  await writePreflightVersionExecutable(
    join(root, executableName),
    liveVersions[adapterId],
    adapterId === "prime/rpc" ? "stderr" : "stdout",
    unexpectedLaunchPath,
  );
}

async function writeCleanupFailingBunRuntime(directory: string): Promise<string> {
  await mkdir(directory, { recursive: true });
  const executable = join(directory, "bun");
  const source = [
    `#!${python}`,
    "import os, time",
    "pid = os.fork()",
    "if pid == 0:",
    "    os.setsid()",
    "    time.sleep(1)",
    "    os._exit(0)",
    "print('1.3.14', flush=True)",
    "os._exit(0)",
  ].join("\n");
  await writeFile(executable, `${source}\n`, { mode: 0o700 });
  await chmod(executable, 0o700);
  return executable;
}

describe("installed adapter launch construction", () => {
  test("keeps the four frozen identities blocked and binds their exact shared bytes", async () => {
    expect(new Set(installedAdapterIds)).toEqual(new Set([
      "codex/exec-json", "claude/print-stream-json", "prime/rpc", "omp/rpc",
    ]));
    for (const id of installedAdapterIds) {
      const definition = installedAdapterDefinition(id);
      expect(definition.strictAdmission).toBe("blocked");
      expect(definition.recipe.admissionClaims).toEqual([]);
      expect(definition.recipe.launch).toMatchObject({ shell: false, outerPty: false, interactionMode: "non-interactive" });
      expect(definition.billingOwner).toBe("user-provider");
      const recipeBytes = new Uint8Array(await readFile(resolve(import.meta.dir, "../../..", scenarios.get(id)!.recipe)));
      expect(definition.recipeSha256).toBe(await sha256(recipeBytes));
    }
  });

  test.each(adapterCases)("reports the frozen %s repair when its executable is missing", async (adapterId) => {
    const definition = installedAdapterDefinition(adapterId);
    const caught = await resolveInstalledExecutable({
      adapterId,
      ambient: {},
      platform: "darwin",
      arch: "arm64",
    }).then(
      () => null,
      (error: unknown) => error as RunnerFailure,
    );
    expect(caught).toMatchObject({
      code: "HARNESS_UNAVAILABLE",
      details: {
        adapterId,
        executableNames: definition.recipe.identity.executableNames,
        admittedVersions: definition.recipe.support.admittedVersions,
        repairCommand: definition.recipe.support.repairCommand,
        fallbackAttempted: false,
      },
    });
    const human = formatHumanError(caught!.toJSON());
    expect(human).toContain(`Admitted versions: ${definition.recipe.support.admittedVersions.join(", ")}`);
    expect(human).toContain(`Repair: ${definition.recipe.support.repairCommand}`);
    expect(human).not.toContain("$PROSE");
  });

  test.each(adapterCases)("builds exact recipe argv and full-image wire bytes for %s", async (adapterId) => {
    const input = await fixture();
    const imagePath = join(input.root, "private prompt.md");
    const taskPath = join(input.root, "private task.json");
    const daemonSocketPath = join(input.root, "prime.sock");
    const renderedConfigPath = join(input.root, "omp-control-overlay.yml");
    const imageBytes = encodeRuntimeImage(input.image);
    const plan = await buildInstalledLaunchPlan({
      adapterId,
      executable: probe,
      invocation: input.invocation,
      imageBytes,
      expectedImageByteLength: input.image.manifest.modelVisibleBytes.byteLength,
      expectedImageSha256: input.image.manifest.modelVisibleBytes.sha256,
      framingTemplateBytes: input.image.files.get(input.image.manifest.oneFieldFraming.path)!,
      imagePath,
      taskPath,
      daemonSocketPath,
      ...(adapterId === "omp/rpc" ? { renderedConfigPath } : {}),
      credentialGroup: credentialGroup(adapterId),
    });
    const taskJson = canonicalJson(input.invocation.task);
    const expectedArgv = scenarios.get(adapterId)!.expectedArgv.map((token) => ({
      "{{EXECUTABLE}}": probe,
      "{{WORKSPACE}}": input.invocation.cwd,
      "{{IMAGE_PATH}}": imagePath,
      "{{IMAGE_UTF8}}": new TextDecoder().decode(imageBytes),
      "{{TASK_JSON}}": taskJson,
      "{{DAEMON_SOCKET_PATH}}": daemonSocketPath,
      "{{RENDERED_CONFIG_PATH}}": renderedConfigPath,
    })[token] ?? token);
    expect(plan.argv).toEqual(expectedArgv);
    expect(plan.stdinLifecycle).toBe(
      scenarios.get(adapterId)!.stdinLifecycle as "close-after-write" | "close-after-terminal-event",
    );
    expect(plan.imageSha256).toBe(input.image.manifest.modelVisibleBytes.sha256);
    expect(imageBytes.byteLength).toBe(input.image.manifest.modelVisibleBytes.byteLength);
    expect(plan).toMatchObject({
      adapterId,
      shell: false,
      outerPty: false,
      billingOwner: "user-provider",
      authCategory: "harness-managed",
      admissionStatus: "blocked",
    });
    const stdin = scenarios.get(adapterId)!.stdin;
    if (stdin === null) expect(plan.stdinBytes).toBeNull();
    else {
      const fixtureBytes = await readFile(resolve(import.meta.dir, "../../..", stdin.fixture));
      const expectedBytes = adapterId === "omp/rpc"
        ? fixtureBytes.subarray(fixtureBytes.indexOf(0x0a) + 1)
        : fixtureBytes;
      expect(Buffer.from(plan.stdinBytes!)).toEqual(expectedBytes);
    }
  });

  test.each(adapterCases)("places the runner-global model at the exact shared %s recipe boundary", async (adapterId) => {
    const input = await fixture();
    const imagePath = join(input.root, "private prompt.md");
    const taskPath = join(input.root, "private task.json");
    const daemonSocketPath = join(input.root, "prime.sock");
    const renderedConfigPath = join(input.root, "omp-control-overlay.yml");
    const imageBytes = encodeRuntimeImage(input.image);
    const taskJson = canonicalJson(input.invocation.task);
    const plan = await buildInstalledLaunchPlan({
      adapterId,
      executable: probe,
      invocation: input.invocation,
      imageBytes,
      expectedImageByteLength: input.image.manifest.modelVisibleBytes.byteLength,
      expectedImageSha256: input.image.manifest.modelVisibleBytes.sha256,
      framingTemplateBytes: input.image.files.get(input.image.manifest.oneFieldFraming.path)!,
      imagePath,
      taskPath,
      daemonSocketPath,
      ...(adapterId === "omp/rpc" ? { renderedConfigPath } : {}),
      credentialGroup: credentialGroup(adapterId),
      model: "fixture-model",
    });
    const expected = scenarios.get(adapterId)!.expectedArgvWithModel.map((token) => ({
      "{{EXECUTABLE}}": probe,
      "{{WORKSPACE}}": input.invocation.cwd,
      "{{IMAGE_PATH}}": imagePath,
      "{{IMAGE_UTF8}}": new TextDecoder().decode(imageBytes),
      "{{TASK_JSON}}": taskJson,
      "{{MODEL}}": "fixture-model",
      "{{DAEMON_SOCKET_PATH}}": daemonSocketPath,
      "{{RENDERED_CONFIG_PATH}}": renderedConfigPath,
    })[token] ?? token);
    expect(plan.argv).toEqual(expected);
  });

  test.each(adapterCases)("passes one explicit credential group and strips every other secret for %s", (adapterId) => {
    const definition = installedAdapterDefinition(adapterId);
    const selected = credentialGroup(adapterId);
    const environment = buildInstalledAdapterEnvironment({
      definition,
      ambient: ambientFor(adapterId),
      credentialGroup: selected,
      controls: { OPENPROSE_ADAPTER_EXPECTED_ID: adapterId },
    });
    expect(environment.PATH).toBeString();
    expect(environment.HOME).toBe("/fixture/home");
    expect(environment.USER).toBe("fixture-posix-user");
    expect(environment.USERPROFILE).toBe("C:\\fixture\\profile");
    expect(environment.OPENPROSE_TOKEN).toBeUndefined();
    expect(environment.PROSE_TOKEN).toBeUndefined();
    expect(environment.RANDOM_AMBIENT).toBeUndefined();
    for (const [group, names] of Object.entries(definition.credentialGroups)) {
      for (const name of names) {
        if (definition.credentialGroups[selected]!.includes(name)) expect(environment[name]).toBe(ambientFor(adapterId)[name]);
        else expect(environment[name]).toBeUndefined();
      }
    }
    expect(environment.OPENPROSE_ADAPTER_EXPECTED_ID).toBe(adapterId);
    expect(environment.PRIME_AGENT_TELEMETRY).toBe(adapterId === "prime/rpc" ? "0" : undefined);
  });

  test("protects every final non-control child value while leaving runner-owned controls public", () => {
    const definition = installedAdapterDefinition("prime/rpc");
    const controls = {
      OPENPROSE_ADAPTER_OBSERVATION_PATH: "/fixture/control-observation.json",
      OPENPROSE_ADAPTER_EXPECTED_ID: "prime/rpc",
    };
    const environment = buildInstalledAdapterEnvironment({
      definition,
      ambient: {
        PATH: "/fixture/bin",
        HOME: "/fixture/home",
        TMPDIR: "/fixture/tmp",
        XDG_CONFIG_HOME: "/fixture/xdg",
        USERPROFILE: "C:\\fixture\\profile",
        SSH_AUTH_SOCK: "/fixture/ssh-agent.sock",
        OPENROUTER_API_KEY: "fixture-selected-key",
      },
      credentialGroup: "openrouter",
      credentialConfigDirectory: "/fixture/private-config",
      controls,
    });
    expect(installedAdapterProtectedValues({
      definition,
      credentialGroup: "openrouter",
      environment,
      controlNames: Object.keys(controls),
    })).toEqual([
      "/fixture/ssh-agent.sock",
      "/fixture/private-config",
      "fixture-selected-key",
      "C:\\fixture\\profile",
      "/fixture/home",
      "/fixture/bin",
      "/fixture/tmp",
      "/fixture/xdg",
    ]);
    expect(installedAdapterProtectedValues({
      definition,
      credentialGroup: "openrouter",
      environment,
      controlNames: Object.keys(controls),
    })).not.toContain("0");
    expect(installedAdapterProtectedValues({
      definition,
      credentialGroup: "openrouter",
      environment,
      controlNames: Object.keys(controls),
    })).not.toContain("/fixture/control-observation.json");
  });

  test("rejects an ambiguous or unknown credential group before spawn", () => {
    expect(() => buildInstalledAdapterEnvironment({
      definition: installedAdapterDefinition("prime/rpc"),
      ambient: {},
      credentialGroup: "auto",
    })).toThrow(/Runner configuration is invalid/u);
  });

  test.each(["prime/rpc", "omp/rpc"] as const)(
    "rejects missing or empty selected credentials for %s without cached-route fallback",
    (adapterId) => {
      const definition = installedAdapterDefinition(adapterId);
      for (const ambient of [
        { PATH: "/bin", ANTHROPIC_API_KEY: "other-provider-must-not-fallback" },
        { PATH: "/bin", OPENROUTER_API_KEY: "", ANTHROPIC_API_KEY: "other-provider-must-not-fallback" },
      ]) {
        expect(() => buildInstalledAdapterEnvironment({
          definition,
          ambient,
          credentialGroup: "openrouter",
        })).toThrow(expect.objectContaining({
          code: "HARNESS_NEEDS_AUTH",
          details: { adapterId, authProfile: "openrouter" },
        }));
      }
    },
  );

  test("keeps native cached-login groups eligible for their harness-owned readiness probes", () => {
    expect(buildInstalledAdapterEnvironment({
      definition: installedAdapterDefinition("codex/exec-json"),
      ambient: { PATH: "/bin", HOME: "/fixture/home", USER: "fixture" },
      credentialGroup: "cached-chatgpt-login",
    })).toEqual({ PATH: "/bin", HOME: "/fixture/home", USER: "fixture" });
    expect(buildInstalledAdapterEnvironment({
      definition: installedAdapterDefinition("claude/print-stream-json"),
      ambient: { PATH: "/bin", HOME: "/fixture/home", USER: "fixture" },
      credentialGroup: "claude-subscription",
    })).toEqual({ PATH: "/bin", HOME: "/fixture/home", USER: "fixture" });
  });

  test.each([
    ["prime/rpc", "prime-harness-login"],
    ["omp/rpc", "omp-harness-login"],
  ] as const)("keeps the explicit %s harness-login route HOME-backed and strips every provider credential", (adapterId, credentialGroup) => {
    const definition = installedAdapterDefinition(adapterId);
    expect(definition.credentialGroups[credentialGroup]).toEqual([]);
    expect(definition.credentialRequirements[credentialGroup]).toEqual({ kind: "probe-owned" });
    const secret = "raw-provider-secret-must-not-appear";
    const ambient = {
      PATH: "/bin",
      HOME: "/fixture/home",
      USER: "fixture",
      ANTHROPIC_API_KEY: secret,
      ANTHROPIC_OAUTH_TOKEN: secret,
      OPENAI_API_KEY: secret,
      OPENROUTER_API_KEY: secret,
      GEMINI_API_KEY: secret,
      GOOGLE_APPLICATION_CREDENTIALS: "/fixture/provider-key.json",
      GOOGLE_CLOUD_PROJECT: "provider-project",
      GOOGLE_CLOUD_LOCATION: "provider-location",
      GITHUB_TOKEN: secret,
      GH_TOKEN: secret,
      COPILOT_GITHUB_TOKEN: secret,
      AWS_ACCESS_KEY_ID: secret,
      AWS_SECRET_ACCESS_KEY: secret,
      AWS_SESSION_TOKEN: secret,
      AWS_PROFILE: "provider-profile",
      AWS_REGION: "provider-region",
      AWS_DEFAULT_REGION: "provider-default-region",
      PRIME_AGENT_CODING_AGENT_DIR: "/fixture/prime-store-override",
      PI_CODING_AGENT_DIR: "/fixture/omp-store-override",
      PRIME_AGENT_TELEMETRY: "1",
    };
    const environment = buildInstalledAdapterEnvironment({ definition, ambient, credentialGroup });
    expect(environment).toEqual({
      PATH: "/bin",
      HOME: "/fixture/home",
      USER: "fixture",
      ...(adapterId === "prime/rpc" ? { PRIME_AGENT_TELEMETRY: "0" } : {}),
    });
    expect(JSON.stringify(environment)).not.toContain(secret);
    expect(JSON.stringify(environment)).not.toContain("store-override");
  });

  test.each([
    ["prime/rpc", "prime-harness-login", "PRIME_AGENT_CODING_AGENT_DIR", "PI_CODING_AGENT_DIR"],
    ["omp/rpc", "omp-harness-login", "PI_CODING_AGENT_DIR", "PRIME_AGENT_CODING_AGENT_DIR"],
  ] as const)("assigns the private %s config directory to every environment-key profile", (adapterId, loginProfile, configName, otherConfigName) => {
    const definition = installedAdapterDefinition(adapterId);
    for (const credentialGroup of Object.keys(definition.credentialGroups)) {
      if (credentialGroup === loginProfile) continue;
      const configDirectory = `/runner-owned/${adapterId.replace("/", "-")}/${credentialGroup}`;
      const environment = buildInstalledAdapterEnvironment({
        definition,
        ambient: ambientFor(adapterId),
        credentialGroup,
        credentialConfigDirectory: configDirectory,
      });
      expect(environment[configName]).toBe(configDirectory);
      expect(environment[otherConfigName]).toBeUndefined();
      expect(environment.PRIME_AGENT_TELEMETRY).toBe(adapterId === "prime/rpc" ? "0" : undefined);
      expect(JSON.stringify(environment)).not.toContain("store-override");
    }
  });

  test("shows detected, admitted, and copyable repair details in human doctor and run failures", async () => {
    const input = await fixture();
    const executable = join(input.root, "codex");
    await writeFile(executable, `#!${process.execPath}\nconsole.log("codex-cli 0.149.0-alpha.4.2");\n`, { mode: 0o700 });
    await chmod(executable, 0o700);
    const execute = async (argv: string[]) => {
      let stdout = "";
      let stderr = "";
      const exit = await runCli(argv, {
        env: { PATH: input.root, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008885" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: (text) => { stderr += text; },
        imageBundle: sentinelImage,
      });
      return { exit, stdout, stderr };
    };
    const expected = [
      "Detected version: codex-cli 0.149.0-alpha.4.2",
      "Admitted versions: 0.149.0-alpha.4.1",
      "Repair: npm install --global @openai/codex@0.149.0-alpha.4.1",
    ];
    const doctor = await execute(["--harness", "codex", "--output", "human", "cli", "doctor"]);
    expect(doctor.exit).toBe(10);
    for (const line of expected) expect(doctor.stdout).toContain(line);
    const run = await execute(["--harness", "codex", "--output", "human", "run", "hello.prose.md"]);
    expect(run.exit).toBe(10);
    for (const line of expected) expect(run.stderr).toContain(line);
  });

  test.each(["prime/rpc", "omp/rpc"] as const)(
    "requires complete AWS and Google credential alternatives for %s",
    (adapterId) => {
      const definition = installedAdapterDefinition(adapterId);
      const control = adapterId === "prime/rpc" ? { PRIME_AGENT_TELEMETRY: "0" } : {};
      for (const ambient of [
        { AWS_REGION: "us-east-1" },
        { AWS_ACCESS_KEY_ID: "access-only", AWS_REGION: "us-east-1" },
        { AWS_SECRET_ACCESS_KEY: "secret-only" },
      ]) {
        expect(() => buildInstalledAdapterEnvironment({ definition, ambient, credentialGroup: "aws-bedrock" }))
          .toThrow(expect.objectContaining({ code: "HARNESS_NEEDS_AUTH" }));
      }
      expect(buildInstalledAdapterEnvironment({ definition, ambient: { AWS_PROFILE: "fixture" }, credentialGroup: "aws-bedrock" }))
        .toEqual({ AWS_PROFILE: "fixture", ...control });
      expect(buildInstalledAdapterEnvironment({
        definition,
        ambient: { AWS_ACCESS_KEY_ID: "access", AWS_SECRET_ACCESS_KEY: "secret", AWS_REGION: "us-east-1" },
        credentialGroup: "aws-bedrock",
      })).toEqual({ AWS_ACCESS_KEY_ID: "access", AWS_SECRET_ACCESS_KEY: "secret", AWS_REGION: "us-east-1", ...control });
      for (const ambient of [{ GOOGLE_CLOUD_PROJECT: "project" }, { GOOGLE_CLOUD_LOCATION: "location" }]) {
        expect(() => buildInstalledAdapterEnvironment({ definition, ambient, credentialGroup: "google" }))
          .toThrow(expect.objectContaining({ code: "HARNESS_NEEDS_AUTH" }));
      }
      expect(buildInstalledAdapterEnvironment({ definition, ambient: { GEMINI_API_KEY: "key" }, credentialGroup: "google" }))
        .toEqual({ GEMINI_API_KEY: "key", ...control });
      expect(buildInstalledAdapterEnvironment({ definition, ambient: { GOOGLE_APPLICATION_CREDENTIALS: "/fixture/key.json" }, credentialGroup: "google" }))
        .toEqual({ GOOGLE_APPLICATION_CREDENTIALS: "/fixture/key.json", ...control });
    },
  );

  test("admits only recipe-declared host targets before probing or launch", () => {
    expect(assertInstalledAdapterPlatform("codex/exec-json", { platform: "linux", arch: "x64" })).toBe("linux-x64-musl");
    expect(assertInstalledAdapterPlatform("omp/rpc", { platform: "linux", arch: "x64" })).toBe("linux-x64");
    expect(() => assertInstalledAdapterPlatform("claude/print-stream-json", { platform: "linux", arch: "x64" }))
      .toThrow(expect.objectContaining({ code: "HARNESS_INCOMPATIBLE" }));
    expect(() => assertInstalledAdapterPlatform("prime/rpc", { platform: "darwin", arch: "x64" }))
      .toThrow(expect.objectContaining({ code: "HARNESS_INCOMPATIBLE" }));
  });

  test("rejects oversized Claude task and Prime total argv without changing prompt placement", async () => {
    const input = await fixture();
    const task: TaskEnvelope = { ...input.invocation.task, argv: ["x".repeat(140_000)] };
    const invocation: RunnerInvocation = {
      ...input.invocation,
      task,
      taskDigestSha256: await sha256(canonicalJson(task)),
    };
    await expect(buildInstalledLaunchPlan({
      adapterId: "claude/print-stream-json",
      executable: probe,
      invocation,
      imageBytes: encodeRuntimeImage(input.image),
      expectedImageByteLength: input.image.manifest.modelVisibleBytes.byteLength,
      expectedImageSha256: input.image.manifest.modelVisibleBytes.sha256,
      framingTemplateBytes: input.image.files.get(input.image.manifest.oneFieldFraming.path)!,
      imagePath: join(input.root, "image"),
      taskPath: join(input.root, "task"),
      credentialGroup: "claude-subscription",
    })).rejects.toMatchObject({ code: "CONFIG_INVALID" });
    expect(() => assertInstalledAdapterArgv("prime/rpc", [
      probe, "--mode", "rpc", "--cwd", "c".repeat(20_000),
      "--append-system-prompt", "i".repeat(125_000), "--model", "m".repeat(125_000),
    ], { platform: "linux", arch: "x64" })).toThrow(expect.objectContaining({ code: "CONFIG_INVALID" }));
    expect(() => assertInstalledAdapterArgv("prime/rpc", [probe, "😀".repeat(4_096)], { platform: "win32", arch: "x64" }))
      .toThrow(expect.objectContaining({ code: "CONFIG_INVALID" }));
  });

  test("keeps POSIX USER and Windows USERPROFILE public while stripping secrets case-insensitively on Windows", () => {
    const environment = buildInstalledAdapterEnvironment({
      definition: installedAdapterDefinition("claude/print-stream-json"),
      ambient: {
        PATH: "C:\\Windows\\System32",
        USER: "fixture-posix-user",
        USERPROFILE: "C:\\Users\\fixture",
        openprose_token: "must-not-pass",
        RANDOM_AMBIENT: "must-not-pass-either",
      },
      credentialGroup: "claude-subscription",
      platform: "win32",
    });
    expect(environment).toEqual({
      PATH: "C:\\Windows\\System32",
      USER: "fixture-posix-user",
      USERPROFILE: "C:\\Users\\fixture",
    });
    expect(buildInstalledAdapterEnvironment({
      definition: installedAdapterDefinition("prime/rpc"),
      ambient: {
        PATH: "C:\\Windows\\System32",
        openrouter_api_key: "fixture-selected-secret",
        anthropic_api_key: "other-provider-must-not-pass",
      },
      credentialGroup: "openrouter",
      platform: "win32",
    })).toEqual({
      PATH: "C:\\Windows\\System32",
      openrouter_api_key: "fixture-selected-secret",
      PRIME_AGENT_TELEMETRY: "0",
    });
  });

  test("rejects model-visible bytes that differ from the verified manifest", async () => {
    const input = await fixture();
    const changed = encodeRuntimeImage(input.image).slice();
    changed[0] = changed[0]! ^ 1;
    await expect(buildInstalledLaunchPlan({
      adapterId: "codex/exec-json",
      executable: probe,
      invocation: input.invocation,
      imageBytes: changed,
      expectedImageByteLength: input.image.manifest.modelVisibleBytes.byteLength,
      expectedImageSha256: input.image.manifest.modelVisibleBytes.sha256,
      framingTemplateBytes: input.image.files.get(input.image.manifest.oneFieldFraming.path)!,
      imagePath: join(input.root, "image"),
      taskPath: join(input.root, "task"),
      credentialGroup: "cached-chatgpt-login",
    })).rejects.toMatchObject({ code: "IMAGE_INVALID" });
  });
});

describe("provider-free installed adapter execution", () => {
  test("serializes caller-controlled private inputs before acquiring an owned root", async () => {
    const input = await fixture();
    const invalidFiles = new Map(input.image.files);
    invalidFiles.delete(input.image.manifest.payload[0]!.path);
    const invalidImage = { ...input.image, files: invalidFiles };
    const observationPath = join(input.root, "invalid-image-must-not-spawn.json");

    await expect(runProviderFreeInstalledAdapter({
      adapterId: "claude/print-stream-json",
      executable: probe,
      observationPath,
      credentialGroup: credentialGroup("claude/print-stream-json"),
      invocation: input.invocation,
      image: invalidImage,
      ambient: ambientFor("claude/print-stream-json"),
      timeoutMs: 2_000,
      fixtureInterpreter: python,
      temporaryRoot: input.root,
    })).rejects.toMatchObject({
      code: "INTERNAL_ERROR",
      details: {
        adapterId: "claude/print-stream-json",
        reason: "Cannot create private installed-adapter transport files.",
      },
    });

    expect(await Bun.file(observationPath).exists()).toBeFalse();
    expect((await readdir(input.root)).filter((entry) =>
      entry.startsWith("openprose-transport-") || entry.startsWith("openprose-prime-"))).toEqual([]);
  });

  test.each(adapterCases)(
    "partial %s creation preserves its error after verified cleanup and lets cleanup failure dominate",
    async (adapterId) => {
      for (const mode of ["creation-failure", "cleanup-failure"] as const) {
        const input = await fixture();
        const observationPath = join(input.root, `${mode}-must-not-spawn.json`);
        const caught = await runProviderFreeInstalledAdapter({
          adapterId,
          executable: probe,
          observationPath,
          credentialGroup: credentialGroup(adapterId),
          invocation: input.invocation,
          image: input.image,
          ambient: {
            ...ambientFor(adapterId),
            OPENPROSE_CONFORMANCE_ADAPTER_CREATION_FAILURE: mode,
          },
          timeoutMs: 2_000,
          fixtureInterpreter: python,
          temporaryRoot: input.root,
        }).then(() => null, (error: unknown) => error);
        expect(await Bun.file(observationPath).exists()).toBeFalse();
        const privateRoots = (await readdir(input.root)).filter((entry) =>
          entry.startsWith("openprose-transport-") || entry.startsWith("openprose-prime-"));
        if (mode === "creation-failure") {
          expect(caught).toMatchObject({
            code: "INTERNAL_ERROR",
            details: {
              adapterId,
              reason: "Cannot create private installed-adapter transport files.",
            },
          });
          expect(JSON.stringify(caught)).not.toContain(input.root);
          expect(privateRoots).toEqual([]);
        } else {
          expect(caught).toMatchObject({
            code: "PROCESS_CLEANUP_FAILED",
            details: {
              phase: "private-file-finalization",
              resource: "owned-private-transport-files",
              adapterId,
              fallbackAttempted: false,
            },
          });
          expect(privateRoots).toHaveLength(1);
          expect(JSON.stringify(caught)).not.toContain(input.root);
        }
      }
    },
  );

  test.each(adapterCases)(
    "private-file cleanup failure dominates both %s success and child failure without leaking authority",
    async (adapterId) => {
      for (const childFailure of [false, true]) {
        const input = await fixture();
        const observationPath = join(input.root, `cleanup-${childFailure ? "child" : "success"}.json`);
        const failingExecutable = join(input.root, "child-failure-harness");
        if (childFailure) {
          await writeFile(failingExecutable, `#!${process.execPath}\nprocess.exit(23);\n`, { mode: 0o700 });
          await chmod(failingExecutable, 0o700);
        }
        const ambient = {
          ...ambientFor(adapterId),
          OPENPROSE_CONFORMANCE_ADAPTER_CLEANUP_FAILURE: "1",
        };
        const caught = await runProviderFreeInstalledAdapter({
          adapterId,
          executable: childFailure ? failingExecutable : probe,
          observationPath,
          credentialGroup: credentialGroup(adapterId),
          invocation: input.invocation,
          image: input.image,
          ambient,
          timeoutMs: 2_000,
          ...(childFailure ? {} : { fixtureInterpreter: python }),
          temporaryRoot: input.root,
        }).then(
          () => null,
          (error: unknown) => error as RunnerFailure,
        );

        expect(caught).toMatchObject({
          code: "PROCESS_CLEANUP_FAILED",
          details: {
            phase: "private-file-finalization",
            resource: "owned-private-transport-files",
            adapterId,
            fallbackAttempted: false,
          },
        });
        expect(caught?.details?.cleanupHandle).toBeUndefined();
        expect(caught?.details?.cleanupArgv).toBeUndefined();
        const rendered = JSON.stringify(caught);
        const human = formatHumanError(caught!.toJSON());
        expect(rendered).not.toContain(input.root);
        expect(rendered).not.toContain("path with spaces/example.prose.md");
        expect(rendered).not.toContain(";$(touch nope)");
        expect(rendered).not.toContain("fixture-secret:");
        expect(rendered).not.toContain("openprose.skill-runtime-image");
        expect(human).toContain("PROCESS_CLEANUP_FAILED");
        for (const forbidden of [
          input.root,
          "path with spaces/example.prose.md",
          ";$(touch nope)",
          "fixture-secret:",
          "openprose.skill-runtime-image",
        ]) expect(human).not.toContain(forbidden);
      }
    },
  );

  test.each(adapterCases)("records exact %s launch and normalizes its harness terminal", async (adapterId) => {
    const input = await fixture();
    const hostileProjectConfig = join(input.invocation.cwd, ".omp", "config.yml");
    const hostileProjectBytes = "retry:\n  enabled: true\nextensions:\n  - hostile-extension\n";
    if (adapterId === "omp/rpc") {
      await mkdir(dirname(hostileProjectConfig), { recursive: true });
      await Bun.write(hostileProjectConfig, hostileProjectBytes);
    }
    const observationPath = join(input.root, `${adapterId.replace("/", "-")}-observation.json`);
    const result = await runProviderFreeInstalledAdapter({
      adapterId,
      executable: probe,
      observationPath,
      credentialGroup: credentialGroup(adapterId),
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor(adapterId),
      timeoutMs: 2_000,
      fixtureInterpreter: python,
    });
    expect(result.process.error).toBeNull();
    expect(result.process.exitCode).toBe(0);
    expect(result.process.terminalEventObserved).toBeTrue();
    expect(result.process.terminalEnvelope).toBeNull();
    expect(result.process.events.map((event) => event.type)).toEqual([
      "session.started", "assistant.message", "session.completed",
    ]);
    expect(result.plan.billingOwner).toBe("user-provider");
    expect(result.plan.admissionStatus).toBe("blocked");

    const observationText = await readFile(observationPath, "utf8");
    const observation = JSON.parse(observationText);
    expect(observation).toMatchObject({
      schema: "openprose.adapter-probe-observation/1",
      adapterId,
      argv: result.plan.argv,
      cwd: input.invocation.cwd,
      shell: false,
      outerPty: false,
    });
    const stdinBytes = adapterId === "omp/rpc"
      ? new Uint8Array(await readFile(resolve(import.meta.dir, "../../..", scenarios.get(adapterId)!.stdin!.fixture)))
      : result.plan.stdinBytes ?? new Uint8Array();
    expect(observation.stdin.byteLength).toBe(stdinBytes.byteLength);
    expect(observation.stdin.sha256).toBe(await sha256(stdinBytes));
    expect(Buffer.from(observation.stdin.base64, "base64")).toEqual(Buffer.from(stdinBytes));
    const expectedEnvironment = buildInstalledAdapterEnvironment({
      definition: installedAdapterDefinition(adapterId),
      ambient: ambientFor(adapterId),
      credentialGroup: credentialGroup(adapterId),
    });
    const credentialConfigName = adapterId === "prime/rpc"
      ? "PRIME_AGENT_CODING_AGENT_DIR"
      : adapterId === "omp/rpc"
        ? "PI_CODING_AGENT_DIR"
        : undefined;
    expect(new Set(observation.environmentNames)).toEqual(new Set([
      ...Object.keys(expectedEnvironment),
      ...(credentialConfigName === undefined ? [] : [credentialConfigName]),
      "OPENPROSE_INVOCATION_ID",
      "OPENPROSE_RECURSION_TOKEN",
      "OPENPROSE_RUN_NONCE",
    ]));
    expect(observationText).not.toContain("fixture-secret:");
    expect(observationText).not.toContain("openprose-secret-must-not-pass");
    expect(observationText).not.toContain("store-override");
    expect(observationText).not.toContain("hostile-omp-config");
    expect(observation.adapterControls).toEqual(
      adapterId === "prime/rpc" ? { PRIME_AGENT_TELEMETRY: "0" } : {},
    );
    if (adapterId === "omp/rpc") {
      const configFile = observation.files.find((file: { flag: string }) => file.flag === "--config");
      expect(observation.argv.slice(-2)).toEqual(["--config", configFile.path]);
      expect(configFile).toMatchObject({ mode: "0600", byteLength: OMP_CONTROL_OVERLAY_BYTES.byteLength });
      expect(Buffer.from(configFile.base64, "base64")).toEqual(Buffer.from(OMP_CONTROL_OVERLAY_BYTES));
      expect(await readFile(hostileProjectConfig, "utf8")).toBe(hostileProjectBytes);
    } else {
      expect(observation.files.find((file: { flag: string }) => file.flag === "--config")).toBeUndefined();
    }
    if (credentialConfigName === undefined) {
      expect(observation.credentialConfig).toBeNull();
    } else {
      expect(observation.credentialConfig).toMatchObject({
        name: credentialConfigName,
        absolute: true,
        existsAtHarnessStart: true,
        ...(process.platform === "win32" ? {} : { mode: "0700" }),
      });
      expect(observation.credentialConfig.path).toEndWith("/credential-config");
      expect(await Bun.file(observation.credentialConfig.path).exists()).toBeFalse();
    }
    for (const promptFile of observation.files.filter((file: { flag: string }) => file.flag !== "--config")) {
      expect(promptFile.mode).toBe("0600");
      expect(promptFile.byteLength).toBe(input.image.manifest.modelVisibleBytes.byteLength);
      expect(promptFile.sha256).toBe(input.image.manifest.modelVisibleBytes.sha256);
      expect(Buffer.from(promptFile.base64, "base64")).toEqual(Buffer.from(encodeRuntimeImage(input.image)));
      expect(await Bun.file(promptFile.path).exists()).toBeFalse();
    }
    if (adapterId === "prime/rpc") {
      expect(observation.daemonSocket).toMatchObject({
        path: result.plan.daemonSocketPath,
        absolute: true,
        existedAtHarnessStart: false,
        ...(process.platform === "win32" ? {} : { parentMode: "0700" }),
      });
      expect(result.plan.argv.filter((value) => value === "--daemon-socket")).toHaveLength(1);
      expect(result.plan.daemonSocketPath).not.toContain(".prime-agent");
      expect(await Bun.file(result.plan.daemonSocketPath!).exists()).toBeFalse();
      expect(await Bun.file(dirname(result.plan.daemonSocketPath!)).exists()).toBeFalse();
    } else {
      expect(observation.daemonSocket).toBeNull();
      expect(result.plan.daemonSocketPath).toBeNull();
    }
  });

  test.each([
    ["prime/rpc", "PRIME_AGENT_CODING_AGENT_DIR"],
    ["omp/rpc", "PI_CODING_AGENT_DIR"],
  ] as const)("gives each %s environment-key run a fresh private config directory through settlement", async (adapterId, configName) => {
    const paths: string[] = [];
    for (let index = 0; index < 2; index += 1) {
      const input = await fixture();
      const observationPath = join(input.root, `${index}-credential-config.json`);
      const result = await runProviderFreeInstalledAdapter({
        adapterId,
        executable: probe,
        observationPath,
        credentialGroup: "openrouter",
        invocation: input.invocation,
        image: input.image,
        ambient: ambientFor(adapterId),
        timeoutMs: 2_000,
        fixtureInterpreter: python,
      });
      expect(result.process.error).toBeNull();
      const observation = JSON.parse(await readFile(observationPath, "utf8"));
      expect(observation.credentialConfig).toMatchObject({
        name: configName,
        absolute: true,
        existsAtHarnessStart: true,
        ...(process.platform === "win32" ? {} : { mode: "0700" }),
      });
      expect(observation.credentialConfig.path).not.toContain("store-override");
      expect(observation.adapterControls).toEqual(
        adapterId === "prime/rpc" ? { PRIME_AGENT_TELEMETRY: "0" } : {},
      );
      expect(await Bun.file(observation.credentialConfig.path).exists()).toBeFalse();
      paths.push(observation.credentialConfig.path);
    }
    expect(new Set(paths).size).toBe(2);
  });

  test("awaits the normalized assistant sink before accepting final process settlement", async () => {
    const input = await fixture();
    let release!: () => void;
    const gate = new Promise<void>((resolve) => { release = resolve; });
    let entered!: () => void;
    const callbackEntered = new Promise<void>((resolve) => { entered = resolve; });
    let callbackText = "";
    let settled = false;
    const run = runProviderFreeInstalledAdapter({
      adapterId: "codex/exec-json",
      executable: probe,
      observationPath: join(input.root, "backpressure-observation.json"),
      credentialGroup: "cached-chatgpt-login",
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor("codex/exec-json"),
      timeoutMs: 2_000,
      fixtureInterpreter: python,
      createAssistantMessageSink: () => async (text) => {
        callbackText = text;
        entered();
        await gate;
      },
    }).finally(() => { settled = true; });

    await callbackEntered;
    expect(callbackText).toContain("Echoed task argv:");
    expect(settled).toBeFalse();
    release();
    expect((await run).process.error).toBeNull();
    expect(settled).toBeTrue();
  });

  test("Prime socket paths are unique and private allocation failure occurs before spawn", async () => {
    const input = await fixture();
    const firstObservation = join(input.root, "prime-socket-first.json");
    const secondObservation = join(input.root, "prime-socket-second.json");
    const run = (observationPath: string, temporaryRoot?: string) => runProviderFreeInstalledAdapter({
      adapterId: "prime/rpc",
      executable: probe,
      observationPath,
      credentialGroup: "openrouter",
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor("prime/rpc"),
      timeoutMs: 2_000,
      fixtureInterpreter: python,
      ...(temporaryRoot === undefined ? {} : { temporaryRoot }),
    });
    const first = await run(firstObservation);
    const second = await run(secondObservation);
    expect(first.plan.daemonSocketPath).not.toBe(second.plan.daemonSocketPath);
    const unusableRoot = join(input.root, "not-a-directory");
    await writeFile(unusableRoot, "fixture");
    const mustNotSpawn = join(input.root, "prime-must-not-spawn.json");
    await expect(run(mustNotSpawn, unusableRoot)).rejects.toBeTruthy();
    expect(await Bun.file(mustNotSpawn).exists()).toBeFalse();
  });

  test("classifies EOF without a required adapter terminal as truncated", async () => {
    const input = await fixture();
    const executable = join(input.root, "incomplete-codex");
    await writeFile(executable, [
      `#!${process.execPath}`,
      'console.log(JSON.stringify({type:"thread.started",thread_id:"fixture-thread"}));',
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    const result = await runProviderFreeInstalledAdapter({
      adapterId: "codex/exec-json",
      executable,
      observationPath: join(input.root, "unused.json"),
      credentialGroup: "cached-chatgpt-login",
      invocation: input.invocation,
      image: input.image,
      ambient: { PATH: process.env.PATH },
      timeoutMs: 2_000,
    });
    expect(result.process.error?.code).toBe("PROTOCOL_TRUNCATED");
    expect(result.process.terminalEventObserved).toBeFalse();
  });

  test.each([
    ["truncated", "PROTOCOL_TRUNCATED", false],
    ["malformed", "PROTOCOL_MALFORMED", true],
  ] as const)("reports only the closed Prime parser state for %s output", async (_label, code, malformed) => {
    const input = await fixture();
    const executable = join(input.root, `diagnostic-prime-${code.toLowerCase()}`);
    const accepted = primeScenario.fakeStdout.slice(0, 13);
    const secret = "candidate-frame-secret-must-not-leak";
    await writeFile(executable, [
      `#!${process.execPath}`,
      `for (const frame of ${JSON.stringify(accepted)}) console.log(JSON.stringify(frame));`,
      ...(malformed ? [`process.stdout.write(${JSON.stringify(`{${secret}\n`)});`] : []),
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);

    const result = await runProviderFreeInstalledAdapter({
      adapterId: "prime/rpc",
      executable,
      credentialGroup: "openrouter",
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor("prime/rpc"),
      timeoutMs: 2_000,
    });

    expect(result.process.error?.code).toBe(code);
    expect(result.process.error?.details).toEqual({
      adapterDiagnostic: {
        schema: "openprose.adapter-diagnostic/1",
        adapterId: "prime/rpc",
        stage: malformed ? "jsonl-framing" : "prime-lifecycle",
        phase: malformed ? "record-boundary" : "await-text-delta-or-end",
        counters: {
          acceptedRecords: 13,
          thinkingDeltas: 3,
          textDeltas: 1,
          saturated: false,
        },
      },
      ...(malformed ? {} : {
        reason: "The process reached EOF without the required harness terminal record.",
        exitCode: 0,
      }),
    });
    expect(JSON.stringify(result.process.error)).not.toContain(secret);
  });

  test("redacts selected credential values from installed-adapter diagnostics", async () => {
    const input = await fixture();
    const executable = join(input.root, "diagnostic-prime");
    await writeFile(executable, [
      `#!${process.execPath}`,
      'console.error(`OPENROUTER_API_KEY=${process.env.OPENROUTER_API_KEY}`);',
      'console.error(`PRIME_AGENT_CODING_AGENT_DIR=${process.env.PRIME_AGENT_CODING_AGENT_DIR} ${process.env.PRIME_AGENT_CODING_AGENT_DIR}`);',
      `for (const frame of ${JSON.stringify(primeScenario.fakeStdout)}) console.log(JSON.stringify(frame));`,
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    const result = await runProviderFreeInstalledAdapter({
      adapterId: "prime/rpc",
      executable,
      observationPath: join(input.root, "unused.json"),
      credentialGroup: "openrouter",
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor("prime/rpc"),
      timeoutMs: 2_000,
    });
    expect(result.process.error).toBeNull();
    expect(result.process.stderr).toContain("OPENROUTER_API_KEY=[REDACTED]");
    expect(result.process.stderr).toContain("PRIME_AGENT_CODING_AGENT_DIR=[REDACTED] [REDACTED]");
    expect(result.process.stderr).not.toContain("fixture-secret:");
    expect(result.process.stderr).not.toContain("openprose-transport-");
  });

  test("closes OMP stdin at agent_end, then fails a missing late acknowledgement as truncated", async () => {
    const input = await fixture();
    const executable = join(input.root, "omp-missing-ack");
    const startupFrames = ompScenario.fakeStdout.slice(0, 3);
    const stateResponse = ompScenario.fakeStdout[3];
    const lifecycleFrames = ompScenario.fakeStdout.slice(4, -1);
    await writeFile(executable, [
      `#!${process.execPath}`,
      "const iterator=process.stdin[Symbol.asyncIterator]();",
      `for (const frame of ${JSON.stringify(startupFrames)}) console.log(JSON.stringify(frame));`,
      "await iterator.next();",
      `console.log(JSON.stringify(${JSON.stringify(stateResponse)}));`,
      "await iterator.next();",
      `for (const frame of ${JSON.stringify(lifecycleFrames)}) console.log(JSON.stringify(frame));`,
      "await iterator.next();",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    const result = await runProviderFreeInstalledAdapter({
      adapterId: "omp/rpc",
      executable,
      credentialGroup: "openrouter",
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor("omp/rpc"),
      timeoutMs: 2_000,
    });
    expect(result.process.exitCode).toBe(0);
    expect(result.process.error?.code).toBe("PROTOCOL_TRUNCATED");
    expect(result.process.terminalEventObserved).toBeFalse();
  });

  test("bounds a legacy EOF-driven RPC harness instead of deadlocking", async () => {
    const input = await fixture();
    const executable = join(input.root, "omp-waits-for-eof");
    await writeFile(executable, [
      `#!${process.execPath}`,
      "let stdin=''; for await (const chunk of process.stdin) stdin += chunk;",
      "console.log(JSON.stringify({type:'ready'}));",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    const started = performance.now();
    const result = await runProviderFreeInstalledAdapter({
      adapterId: "omp/rpc",
      executable,
      credentialGroup: "openrouter",
      invocation: input.invocation,
      image: input.image,
      ambient: ambientFor("omp/rpc"),
      timeoutMs: 250,
    });
    expect(result.process.error?.code).toBe("STARTUP_TIMEOUT");
    expect(performance.now() - started).toBeLessThan(1_500);
  });

  test.each([
    ["codex", "exec-json", "cached-chatgpt-login"],
    ["claude", "print-stream-json", "claude-subscription"],
    ["prime", "rpc", "openrouter"],
    ["omp", "rpc", "openrouter"],
  ] as const)("black-box %s/%s probe ends at semantic unknown without admission", async (harness, transport, group) => {
    const input = await fixture();
    const adapterId = `${harness}/${transport}` as InstalledAdapterId;
    if (adapterId === "omp/rpc") await writeBunRuntime(input.root, "1.3.14");
    const observationPath = join(input.root, "cli-observation.json");
    let stdout = "";
    let stderr = "";
    const dependencies: CliDependencies = {
      env: {
        ...ambientFor(adapterId),
        ...(adapterId === "omp/rpc" ? { PATH: input.root } : {}),
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009999" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
      installedAdapterProbe: {
        executable: probe,
        observationPath,
        credentialGroup: group,
        interpreter: python,
      },
    };
    expect(await runCli([
      "--harness", harness, "--transport", transport, "--timeout", "2s", "--output", "json", "run", "fixture.prose.md",
    ], dependencies)).toBe(23);
    expect(stderr).toBe("");
    expect(JSON.parse(stdout)).toMatchObject({
      schema: "openprose.runner-result/1",
      adapter: { id: adapterId, harnessVersion: null },
      negotiatedCapabilities: {
        promptPlacement: installedAdapterDefinition(adapterId).recipe.launch.instructionPlacement.manifestPlacementId,
        isolation: installedAdapterDefinition(adapterId).recipe.isolation.guarantee,
        streaming: "structured",
        cancellation: process.platform === "win32" ? "unsupported" : "process-group-best-effort",
        terminal: "structured",
      },
      terminal: {
        classification: "runner-error",
        transportCompleted: true,
        terminalEventObserved: true,
        exitCode: 0,
        signal: null,
      },
      semantic: { status: "unknown", terminalEnvelopeDigestSha256: null },
      billing: { owner: "user-provider", authCategory: "harness-managed" },
      runnerExitCode: 23,
      error: {
        code: "SEMANTIC_STATUS_UNKNOWN",
        details: {
          adapterId,
          admissionStatus: "blocked",
          transportTerminalObserved: true,
          fallbackAttempted: false,
        },
      },
    });
    expect(await Bun.file(observationPath).exists()).toBeTrue();
  });

  test("a dry run checks readiness without invoking the private provider-free seam", async () => {
    const input = await fixture();
    const observationPath = join(input.root, "must-not-exist.json");
    let stdout = "";
    const dependencies: CliDependencies = {
      env: ambientFor("codex/exec-json"),
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009998" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      installedAdapterProbe: {
        executable: probe,
        observationPath,
        credentialGroup: "cached-chatgpt-login",
        interpreter: python,
      },
    };
    expect(await runCli([
      "--harness", "codex", "--transport", "exec-json", "--dry-run", "--output", "json", "run",
    ], dependencies)).toBe(0);
    expect(JSON.parse(stdout)).toMatchObject({
      schema: "openprose.runner-dry-run-report/1",
      readiness: "ready",
      blockingError: null,
      wouldStartModel: false,
      selection: { adapterId: "codex/exec-json" },
      prompt: { placement: "user-prefix-framed", strictness: "degraded" },
      isolation: "unsupported",
      auth: { category: "harness-managed", readiness: "unknown" },
      billingOwner: "user-provider",
    });
    expect(await Bun.file(observationPath).exists()).toBeFalse();
  });

  test("the closed provider-free environment profile keeps a Prime dry run descriptive", async () => {
    const input = await fixture();
    const observationPath = join(input.root, "prime-dry-run-must-not-exist.json");
    let stdout = "";
    const dependencies: CliDependencies = {
      env: {
        ...ambientFor("prime/rpc"),
        OPENPROSE_CONFORMANCE_ADAPTER_MODE: "provider-free-v1",
        OPENPROSE_CONFORMANCE_ADAPTER_PROBE: probe,
        OPENPROSE_CONFORMANCE_ADAPTER_OBSERVATION: observationPath,
        OPENPROSE_CONFORMANCE_ADAPTER_CREDENTIAL_GROUP: "openrouter",
        OPENPROSE_ADAPTER_EXPECTED_ID: "prime/rpc",
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009997" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
    };
    expect(await runCli([
      "--harness", "prime",
      "--transport", "rpc",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "--dry-run",
      "--output", "json",
      "run", "fixture.prose.md",
    ], dependencies)).toBe(0);
    expect(JSON.parse(stdout)).toMatchObject({
      schema: "openprose.runner-dry-run-report/1",
      wouldStartModel: false,
      selection: {
        harness: "prime",
        transport: "rpc",
        adapterId: "prime/rpc",
        runtimeVersion: null,
        model: "fixture/model",
      },
      prompt: { placement: "system-append", strictness: "strict" },
      isolation: "partial",
      auth: { category: "harness-managed", readiness: "unknown" },
      billingOwner: "user-provider",
      readiness: "ready",
      blockingError: null,
    });
    expect(await Bun.file(observationPath).exists()).toBeFalse();
  });

});

describe("installed executable discovery and version probes", () => {
  const versions: Record<InstalledAdapterId, string> = {
    "agents-sdk/jsonl":"prose-agents-sdk 0.1.0",
    "codex/exec-json": "codex-cli 0.149.0-alpha.4.1",
    "claude/print-stream-json": "2.1.243 (Claude Code)",
    "prime/rpc": "prime-agent 0.8.1",
    "omp/rpc": "omp/18.0.9",
  };

  test.each([
    ["prime", "rpc", "prime/rpc", ["--model", "fixture/model"]],
    ["omp", "rpc", "omp/rpc", ["--model", "fixture/model"]],
    ["codex", "exec-json", "codex/exec-json", []],
    ["claude", "print-stream-json", "claude/print-stream-json", []],
  ] as const)("rejects an unsupported %s auth profile before executable discovery", async (harness, transport, adapterId, modelArgs) => {
    const input = await fixture();
    let stdout = "";
    expect(await runCli([
      "--harness", harness,
      "--transport", transport,
      ...modelArgs,
      "--auth-profile", "unsupported-profile",
      "--output", "json",
      "run", "hello.prose.md",
    ], {
      env: { PATH: join(input.root, "empty-path") },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 0 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009990" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(2);
    expect(JSON.parse(stdout)).toMatchObject({
      error: {
        code: "CONFIG_INVALID",
        details: {
          adapterId,
          reason: `Unknown auth_profile for ${adapterId}: unsupported-profile.`,
          supportedAuthProfiles: Object.keys(installedAdapterDefinition(adapterId).credentialGroups),
        },
      },
    });
  });

  test.each(["prime", "omp"] as const)("preserves %s model validation ahead of auth-profile validation", async (harness) => {
    const input = await fixture();
    let stdout = "";
    expect(await runCli([
      "--harness", harness,
      "--transport", "rpc",
      "--model", "unqualified-model",
      "--auth-profile", "unsupported-profile",
      "--output", "json",
      "run", "hello.prose.md",
    ], {
      env: { PATH: join(input.root, "empty-path") },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 0 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009991" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(2);
    expect(JSON.parse(stdout)).toMatchObject({
      error: {
        code: "CONFIG_INVALID",
        details: {
          adapterId: `${harness}/rpc`,
          reason: "Prime and OMP models must be a fully qualified provider/model with no empty, whitespace, or control-character segments.",
        },
      },
    });
  });

  test("classifies the closed Bun prerequisite version matrix without exposing hostile output", async () => {
    const input = await fixture();
    const requirement = installedAdapterDefinition("omp/rpc").runtimePrerequisites[0]!;
    const missing = await inspectInstalledAdapterRuntimePrerequisites({
      adapterId: "omp/rpc",
      cwd: input.invocation.cwd,
      ambient: { PATH: join(input.root, "missing-runtime") },
    });
    expect(missing).toEqual([{ ...requirement, detectedVersion: null, availability: "missing" }]);

    for (const [output, availability, detectedVersion] of [
      ["1.3.13", "incompatible", "1.3.13"],
      ["1.3.14", "available", "1.3.14"],
      ["1.3.15", "available", "1.3.15"],
      ["1.4.0", "available", "1.4.0"],
      ["2.0.0", "available", "2.0.0"],
      ["1000000.1000000.1000000", "available", "1000000.1000000.1000000"],
      ["1000001.3.14", "incompatible", null],
      ["1.1000001.14", "incompatible", null],
      ["1.3.1000001", "incompatible", null],
      ["1.3.14-beta.1", "incompatible", "1.3.14-beta.1"],
      ["1000000.0.0-beta.1", "incompatible", "1000000.0.0-beta.1"],
      ["01.3.14", "incompatible", null],
      ["malformed $(touch must-not-exist) fixture-secret", "incompatible", null],
    ] as const) {
      const directory = join(input.root, `bun-${output.replaceAll(/[^0-9A-Za-z]+/gu, "-")}`);
      await writeBunRuntime(directory, output);
      const observation = await inspectInstalledAdapterRuntimePrerequisites({
        adapterId: "omp/rpc",
        cwd: input.invocation.cwd,
        ambient: { PATH: directory },
      });
      expect(observation).toEqual([{ ...requirement, detectedVersion, availability }]);
      expect(JSON.stringify(observation)).not.toContain(input.root);
      expect(JSON.stringify(observation)).not.toContain("fixture-secret");
      expect(JSON.stringify(observation)).not.toContain("touch must-not-exist");
    }
    // This outer ceiling covers one missing lookup plus thirteen sequential
    // subprocess observations. Each observation retains the production 5 s
    // probe deadline, so a stuck child still fails closed independently.
  }, 75_000);

  test.skipIf(process.platform === "win32")("preserves runtime-probe cleanup authority through inspection, inventory, doctor, and run", async () => {
    const input = await fixture();
    const commandCases = [
      ["--output", "json", "cli", "harness", "list"],
      ["--harness", "omp", "--model", "fixture/model", "--auth-profile", "openrouter", "--output", "json", "cli", "doctor"],
      ["--harness", "omp", "--transport", "rpc", "--model", "fixture/model", "--auth-profile", "openrouter", "--output", "json", "run", "hello.prose.md"],
    ] as const;

    const directDirectory = join(input.root, "cleanup-direct");
    await writeCleanupFailingBunRuntime(directDirectory);
    await expect(inspectInstalledAdapterRuntimePrerequisites({
      adapterId: "omp/rpc",
      cwd: input.invocation.cwd,
      ambient: { PATH: directDirectory },
    })).rejects.toMatchObject({ code: "PROCESS_CLEANUP_FAILED" });

    for (const [index, argv] of commandCases.entries()) {
      const directory = join(input.root, `cleanup-command-${index}`);
      await writeCleanupFailingBunRuntime(directory);
      await writeFile(join(directory, "omp"), `#!${process.execPath}\nconsole.log('must not run');\n`, { mode: 0o700 });
      let stdout = "";
      let stderr = "";
      const exitCode = await runCli(argv, {
        env: { PATH: directory },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 0 },
        ids: { invocationId: () => `00000000-0000-7000-8000-00000000900${index}` },
        writeStdout: (text) => { stdout += text; },
        writeStderr: (text) => { stderr += text; },
      });
      expect(exitCode).toBe(25);
      expect(stderr).toBe("");
      const report = JSON.parse(stdout);
      const error = report.error ?? report;
      expect(error).toMatchObject({ code: "PROCESS_CLEANUP_FAILED", boundary: "cleanup" });
      expect(report).not.toHaveProperty("schema", "openprose.harness-list/1");
      expect(report).not.toHaveProperty("schema", "openprose.doctor-report/1");
      const rendered = JSON.stringify(error);
      expect(rendered).not.toContain(input.root);
      expect(rendered).not.toContain("hello.prose.md");
    }
    await Bun.sleep(1_100);
  }, SERIAL_RUNTIME_CLEANUP_TEST_TIMEOUT_MS);

  test("uses the first Bun on ordered PATH with a credential-free probe and accepts separate OMP/Bun directories", async () => {
    const input = await fixture();
    const oldDirectory = join(input.root, "old-bun");
    const goodDirectory = join(input.root, "good-bun");
    const oldObservation = join(input.root, "old-bun-observation.json");
    const goodObservation = join(input.root, "good-bun-observation.json");
    await writeBunRuntime(oldDirectory, "1.3.13", oldObservation);
    await writeBunRuntime(goodDirectory, "1.3.14", goodObservation);
    const ambient = {
      PATH: [oldDirectory, goodDirectory].join(delimiter),
      HOME: "/secret/home",
      XDG_CONFIG_HOME: "/secret/xdg",
      SSH_AUTH_SOCK: "/secret/ssh",
      HTTPS_PROXY: "https://secret-proxy.invalid",
      OPENROUTER_API_KEY: "fixture-secret",
      LANG: "C.UTF-8",
      TMPDIR: input.root,
    };
    const caught = await assertInstalledAdapterRuntimePrerequisites({
      adapterId: "omp/rpc",
      cwd: input.invocation.cwd,
      ambient,
    }).then(() => null, (error: unknown) => error as RunnerFailure);
    expect(caught).toMatchObject({
      code: "HARNESS_INCOMPATIBLE",
      details: {
        adapterId: "omp/rpc",
        fallbackAttempted: false,
        runtimePrerequisite: {
          runtime: "bun",
          versionRange: ">=1.3.14",
          detectedVersion: "1.3.13",
          availability: "incompatible",
          repairCommand: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
        },
      },
    });
    expect(await Bun.file(goodObservation).exists()).toBeFalse();
    expect(JSON.parse(await readFile(oldObservation, "utf8"))).toEqual({
      argv: ["--version"],
      environmentNames: [
        "LANG",
        "OPENPROSE_INVOCATION_ID",
        "OPENPROSE_RECURSION_TOKEN",
        "OPENPROSE_RUN_NONCE",
        "PATH",
        "TMPDIR",
      ],
    });
    const rendered = JSON.stringify(caught);
    for (const forbidden of [input.root, "/secret/home", "/secret/xdg", "/secret/ssh", "secret-proxy", "fixture-secret"]) {
      expect(rendered).not.toContain(forbidden);
    }

    const ompDirectory = join(input.root, "omp-only");
    await mkdir(ompDirectory);
    const accepted = await assertInstalledAdapterRuntimePrerequisites({
      adapterId: "omp/rpc",
      cwd: input.invocation.cwd,
      ambient: { PATH: [ompDirectory, goodDirectory].join(delimiter) },
    });
    expect(accepted[0]).toMatchObject({ availability: "available", detectedVersion: "1.3.14" });
    // This outer ceiling covers two sequential runtime probes. Each retains
    // the production 5 s deadline and its normal fail-closed cleanup path.
  }, SERIAL_PATH_AUTHORITY_TEST_TIMEOUT_MS);

  test("rejects an old Bun before OMP, credential readiness, or environment construction", async () => {
    const input = await fixture();
    const ompDirectory = join(input.root, "omp-bin");
    const bunDirectory = join(input.root, "bun-bin");
    const ompMarker = join(input.root, "omp-must-not-run");
    await mkdir(ompDirectory);
    const ompExecutable = join(ompDirectory, "omp");
    await writeFile(ompExecutable, [
      `#!${process.execPath}`,
      `await Bun.write(${JSON.stringify(ompMarker)}, "started");`,
      "console.log('omp/18.0.9');",
      "",
    ].join("\n"), { mode: 0o700 });
    await chmod(ompExecutable, 0o700);
    await writeBunRuntime(bunDirectory, "1.3.13");
    let stdout = "";
    const dependencies: CliDependencies = {
      env: { PATH: [ompDirectory, bunDirectory].join(delimiter) },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 0 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008888" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
    };
    const exitCode = await runCli([
      "--harness", "omp",
      "--transport", "rpc",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "--output", "json",
      "run", "hello.prose.md",
    ], dependencies);
    expect(exitCode).toBe(10);
    expect(await Bun.file(ompMarker).exists()).toBeFalse();
    expect(JSON.parse(stdout)).toMatchObject({
      error: {
        code: "HARNESS_INCOMPATIBLE",
        details: {
          runtimePrerequisite: { detectedVersion: "1.3.13", availability: "incompatible" },
        },
      },
    });
    stdout = "";
    expect(await runCli([
      "--harness", "omp",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "--output", "json",
      "cli", "doctor",
    ], dependencies)).toBe(10);
    const doctor = JSON.parse(stdout);
    expect(doctor.problems[0]).toMatchObject({
      code: "HARNESS_INCOMPATIBLE",
      details: {
        runtimePrerequisite: { detectedVersion: "1.3.13", availability: "incompatible" },
      },
    });
    expect(doctor.harnesses.find((item: { id: string }) => item.id === "omp").runtimePrerequisites).toEqual([{
      runtime: "bun",
      versionRange: ">=1.3.14",
      detectedVersion: "1.3.13",
      availability: "incompatible",
      repairCommand: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9",
    }]);
    stdout = "";
    expect(await runCli([
      "--harness", "omp",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "cli", "doctor",
    ], dependencies)).toBe(10);
    expect(stdout).toContain("Runtime prerequisite: bun");
    expect(stdout).toContain("Detected runtime version: 1.3.13");
    expect(stdout).toContain("Required runtime version: >=1.3.14");
    expect(stdout).toContain("Repair: npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9");
    expect(stdout).not.toContain(input.root);
    expect(await Bun.file(ompMarker).exists()).toBeFalse();
  });

  test.each(adapterCases)("resolves and admits only the frozen %s version pattern", async (adapterId) => {
    const input = await fixture();
    const name = installedAdapterDefinition(adapterId).recipe.identity.executableNames[0]!;
    const executable = join(input.root, name);
    const environmentCapture = join(input.root, `${name}-version-environment.json`);
    await writeFile(executable, [
      `#!${process.execPath}`,
      `await Bun.write(${JSON.stringify(environmentCapture)}, JSON.stringify(Object.keys(process.env).sort()));`,
      `if (process.argv.includes("--version")) ${adapterId === "prime/rpc" ? "console.error" : "console.log"}(${JSON.stringify(versions[adapterId])});`,
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    expect((await lstat(executable)).mode & 0o111).not.toBe(0);
    const resolved = await resolveInstalledExecutable({ adapterId, ambient: { PATH: input.root } });
    expect(resolved).toBe(await realpath(executable));
    expect(await probeInstalledAdapterVersion({
      adapterId,
      executable: resolved,
      cwd: input.invocation.cwd,
      ambient: {
        PATH: input.root,
        TMPDIR: join(input.root, "tmp"),
        LANG: "C.UTF-8",
        HOME: join(input.root, "hostile-home"),
        USERPROFILE: "C:\\hostile-profile",
        XDG_CONFIG_HOME: join(input.root, "hostile-config"),
        HTTPS_PROXY: "https://proxy-user:proxy-password@example.invalid",
        OPENAI_API_KEY: "version-probe-openai-secret",
        ANTHROPIC_OAUTH_TOKEN: "version-probe-anthropic-secret",
        PRIME_AGENT_TELEMETRY: "1",
        PI_CONFIG_FILES: join(input.root, "hostile-omp.yml"),
        RANDOM_AMBIENT: "version-probe-random-value",
      },
    })).toBe(versions[adapterId]);
    expect(JSON.parse(await readFile(environmentCapture, "utf8"))).toEqual([
      "LANG",
      "OPENPROSE_INVOCATION_ID",
      "OPENPROSE_RECURSION_TOKEN",
      "OPENPROSE_RUN_NONCE",
      "PATH",
      "TMPDIR",
    ]);
  });

  test.each([
    ["prime", "prime/rpc", "prime-agent", "prime-agent 0.8.2"],
    ["omp", "omp/rpc", "omp", "omp/18.0.10"],
  ] as const)("treats the first incompatible %s PATH executable as authoritative without exposing credentials", async (harness, adapterId, executableName, incompatibleVersion) => {
    const input = await fixture();
    const earlier = join(input.root, "hostile-earlier");
    const later = join(input.root, "admitted-later");
    await mkdir(earlier);
    await mkdir(later);
    if (adapterId === "omp/rpc") await writeBunRuntime(earlier, "1.3.14");
    const versionEnvironment = join(input.root, `${harness}-incompatible-version-environment.json`);
    const fallbackMarker = join(input.root, `${harness}-fallback-called`);
    const earlyExecutable = join(earlier, executableName);
    await writeFile(earlyExecutable, [
      `#!${process.execPath}`,
      `await Bun.write(${JSON.stringify(versionEnvironment)}, JSON.stringify(Object.keys(process.env).sort()));`,
      `${adapterId === "prime/rpc" ? "console.error" : "console.log"}(${JSON.stringify(incompatibleVersion)});`,
    ].join("\n"), { mode: 0o700 });
    await chmod(earlyExecutable, 0o700);
    const lateExecutable = join(later, executableName);
    await writeFile(lateExecutable, [
      `#!${process.execPath}`,
      `await Bun.write(${JSON.stringify(fallbackMarker)}, "called");`,
      `${adapterId === "prime/rpc" ? "console.error" : "console.log"}(${JSON.stringify(liveVersions[adapterId])});`,
    ].join("\n"), { mode: 0o700 });
    await chmod(lateExecutable, 0o700);

    const secret = `${harness}-selected-secret-must-not-reach-version-probe`;
    let stdout = "";
    expect(await runCli([
      "--harness", harness,
      "--transport", "rpc",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "--output", "json",
      "cli", "doctor",
    ], {
      env: {
        PATH: `${earlier}${delimiter}${later}`,
        HOME: join(input.root, "cached-login-home"),
        XDG_CONFIG_HOME: join(input.root, "hostile-config"),
        HTTPS_PROXY: "https://proxy-user:proxy-password@example.invalid",
        OPENROUTER_API_KEY: secret,
        PRIME_AGENT_TELEMETRY: "1",
        PI_CONFIG_FILES: join(input.root, "hostile-omp.yml"),
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009991" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(10);
    expect(JSON.parse(stdout)).toMatchObject({
      ready: false,
      selectedHarness: harness,
      problems: [{
        code: "HARNESS_INCOMPATIBLE",
        details: { adapterId, detectedVersion: incompatibleVersion, fallbackAttempted: false },
      }],
    });
    expect(JSON.parse(await readFile(versionEnvironment, "utf8"))).toEqual([
      "OPENPROSE_INVOCATION_ID",
      "OPENPROSE_RECURSION_TOKEN",
      "OPENPROSE_RUN_NONCE",
      "PATH",
    ]);
    expect(stdout).not.toContain(secret);
    expect(await Bun.file(fallbackMarker).exists()).toBeFalse();
  }, SERIAL_PATH_AUTHORITY_TEST_TIMEOUT_MS);

  test.each([
    ["prime/rpc", "prime-agent 0.7.1", "prime-agent", ["0.7.0", "0.8.1"], "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1"],
    ["prime/rpc", "prime-agent 0.8.2", "prime-agent", ["0.7.0", "0.8.1"], "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1"],
    ["omp/rpc", "omp/18.0.10", "omp", ["18.0.9"], "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9"],
    ["codex/exec-json", "codex-cli 0.149.0-alpha.4.2", "codex", ["0.149.0-alpha.4.1"], "npm install --global @openai/codex@0.149.0-alpha.4.1"],
    ["codex/exec-json", "codex-cli 0.150.0-alpha.1", "codex", ["0.149.0-alpha.4.1"], "npm install --global @openai/codex@0.149.0-alpha.4.1"],
    ["claude/print-stream-json", "2.1.244 (Claude Code)", "claude", ["2.1.243"], "npm install --global @anthropic-ai/claude-code@2.1.243"],
    ["claude/print-stream-json", "2.2.0-alpha.1 (Claude Code)", "claude", ["2.1.243"], "npm install --global @anthropic-ai/claude-code@2.1.243"],
  ] as const)("rejects adjacent non-allowlisted %s version %s with exact repair metadata", async (adapterId, version, executableName, admittedVersions, repairCommand) => {
    const input = await fixture();
    const executable = join(input.root, executableName);
    const emit = adapterId === "prime/rpc" ? "console.error" : "console.log";
    await writeFile(executable, `#!${process.execPath}\n${emit}(${JSON.stringify(version)});\n`, { mode: 0o700 });
    await chmod(executable, 0o700);
    await expect(probeInstalledAdapterVersion({
      adapterId,
      executable,
      cwd: input.invocation.cwd,
      ambient: { PATH: process.env.PATH },
    })).rejects.toMatchObject({
      code: "HARNESS_INCOMPATIBLE",
      details: {
        adapterId,
        detectedVersion: version,
        admittedVersions,
        repairCommand,
        fallbackAttempted: false,
      },
    });
  });

  test.each([
    ["prime", "rpc", "prime/rpc", "prime-agent", "prime-agent 0.7.0", ["0.7.0", "0.8.1"], "curl -fsSL https://app.primeintellect.ai/prime-agent/install.sh | sh -s -- 0.8.1", "prime-harness-login"],
    ["omp", "rpc", "omp/rpc", "omp", "omp/18.0.9", ["18.0.9"], "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9", "omp-harness-login"],
    ["codex", "exec-json", "codex/exec-json", "codex", "codex-cli 0.149.0-alpha.4.1", ["0.149.0-alpha.4.1"], "npm install --global @openai/codex@0.149.0-alpha.4.1", null],
    ["claude", "print-stream-json", "claude/print-stream-json", "claude", "2.1.243 (Claude Code)", ["2.1.243"], "npm install --global @anthropic-ai/claude-code@2.1.243", null],
  ] as const)("maps a successful wrong-stream %s version probe to exact safe repair metadata", async (harness, transport, adapterId, executableName, version, admittedVersions, repairCommand, authProfile) => {
    const input = await fixture();
    const executable = join(input.root, executableName);
    const privateOutput = `${harness}-wrong-stream-private-output`;
    const emit = adapterId === "prime/rpc" ? "console.log" : "console.error";
    await writeFile(executable, [
      `#!${process.execPath}`,
      `${emit}(${JSON.stringify(version)});`,
      `${emit}(${JSON.stringify(privateOutput)});`,
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    if (adapterId === "omp/rpc") await writeBunRuntime(input.root, "1.3.14");

    await expect(probeInstalledAdapterVersion({
      adapterId,
      executable,
      cwd: input.invocation.cwd,
      ambient: { PATH: input.root },
    })).rejects.toMatchObject({
      code: "HARNESS_INCOMPATIBLE",
      details: { adapterId, admittedVersions, repairCommand, fallbackAttempted: false },
    });

    const base = ["--harness", harness, "--transport", transport];
    if (authProfile !== null) base.push("--model", "fixture/model", "--auth-profile", authProfile);
    const execute = async (output: "human" | "json") => {
      let stdout = "";
      let stderr = "";
      const exit = await runCli([...base, "--output", output, "cli", "doctor"], {
        env: { PATH: input.root, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008885" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: (text) => { stderr += text; },
        imageBundle: sentinelImage,
      });
      return { exit, stdout, stderr };
    };

    const machine = await execute("json");
    expect(machine.exit).toBe(10);
    expect(JSON.parse(machine.stdout).problems[0].details).toEqual({
      adapterId,
      admittedVersions,
      repairCommand,
      fallbackAttempted: false,
    });
    expect(machine.stderr).toBe("");

    const human = await execute("human");
    expect(human.exit).toBe(10);
    expect(human.stdout).toContain(`Repair: ${repairCommand}`);
    expect(human.stdout).toContain("Run the exact Repair command reported with this error, then retry.");
    expect(human.stderr).toBe("");
    for (const text of [machine.stdout, machine.stderr, human.stdout, human.stderr]) {
      expect(text).not.toContain(privateOutput);
      expect(text).not.toContain(executable);
      expect(text).not.toContain("processExit");
      expect(text).not.toContain("terminalEventObserved");
    }
  });

  test("refuses a missing selected Prime credential before auth readiness or cached-route fallback", async () => {
    const input = await fixture();
    const executable = join(input.root, "prime-agent");
    const started = join(input.root, "unexpected-probe.txt");
    await writeFile(executable, [
      `#!${process.execPath}`,
      `if (process.argv.includes("--version")) { console.error("0.7.0"); process.exit(0); }`,
      `await Bun.write(${JSON.stringify(started)}, "started");`,
      `console.error("unexpected non-version probe");`,
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    let stdout = "";
    expect(await runCli([
      "--harness", "prime",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "--output", "json",
      "cli", "doctor",
    ], {
      env: {
        PATH: input.root,
        HOME: join(input.root, "home"),
        USER: "fixture-user",
        ANTHROPIC_API_KEY: "other-provider-must-not-fallback",
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009996" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(10);
    expect(JSON.parse(stdout)).toMatchObject({
      ready: false,
      selectedHarness: "prime",
      problems: [{ code: "HARNESS_NEEDS_AUTH", details: { adapterId: "prime/rpc", authProfile: "openrouter" } }],
    });
    expect(await Bun.file(started).exists()).toBeFalse();
  });

  test.each([
    ["prime", "prime/rpc", "prime-harness-login"],
    ["omp", "omp/rpc", "omp-harness-login"],
  ] as const)("requires explicit --auth-profile for the %s %s route %s", async (harness, adapterId, credentialGroup) => {
    const input = await fixture();
    await writeLiveHarness(
      input.root,
      adapterId,
      "unused",
      join(input.root, `${harness}-explicit-profile-observation.json`),
    );
    let stdout = "";
    expect(await runCli([
      "--harness", harness,
      "--model", "fixture/model",
      "--output", "json",
      "cli", "doctor",
    ], {
      env: { PATH: input.root, HOME: join(input.root, "home"), USER: "fixture" },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000009995" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(2);
    expect(JSON.parse(stdout)).toMatchObject({
      ready: false,
      selectedHarness: harness,
      selectedAdapterId: adapterId,
      problems: [{
        code: "CONFIG_INVALID",
        details: { adapterId },
      }],
    });
    expect(stdout).toContain(credentialGroup);
  });

  test.each(["prime", "omp"] as const)("rejects ambiguous unqualified %s models before discovery", async (harness) => {
    const input = await fixture();
    for (const model of ["unqualified-model", "/model", "provider/", "provider//model", "provider/model name", "provider/model\u0001name"]) {
      let stdout = "";
      expect(await runCli([
        "--harness", harness,
        "--model", model,
        "--auth-profile", `${harness}-harness-login`,
        "--output", "json",
        "cli", "doctor",
      ], {
        env: { PATH: input.root, HOME: join(input.root, "home"), USER: "fixture" },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000009994" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: () => {},
        imageBundle: sentinelImage,
      })).toBe(2);
      expect(JSON.parse(stdout)).toMatchObject({
        ready: false,
        problems: [{ code: "CONFIG_INVALID" }],
      });
      expect(stdout).toContain("provider/model");
    }
  }, 10_000);

  test.each(["prime/rpc", "omp/rpc"] as const)(
    "refuses standalone %s auth probes because actual execution is first authority",
    async (adapterId) => {
      const input = await fixture();
      const executable = join(input.root, installedAdapterDefinition(adapterId).recipe.identity.executableNames[0]!);
      const marker = join(input.root, "unexpected-auth-probe");
      await writeFile(executable, `#!${process.execPath}\nawait Bun.write(${JSON.stringify(marker)}, "called");\n`, { mode: 0o700 });
      await chmod(executable, 0o700);
      await expect(probeInstalledAdapterAuth({
        adapterId,
        executable,
        cwd: input.invocation.cwd,
        credentialGroup: adapterId === "prime/rpc" ? "prime-harness-login" : "omp-harness-login",
        environment: { PATH: process.env.PATH ?? dirname(process.execPath) },
      })).rejects.toMatchObject({ code: "INTERNAL_ERROR" });
      expect(await Bun.file(marker).exists()).toBeFalse();
    },
  );

  test("keeps failed auth-probe stderr out of machine-readable errors", async () => {
    const input = await fixture();
    const executable = join(input.root, "codex");
    const providerBody = "provider-body-must-not-appear";
    const secret = "selected-secret-must-not-appear";
    await writeFile(executable, [
      `#!${process.execPath}`,
      `console.error(${JSON.stringify(`${providerBody}:${secret}`)});`,
      "process.exit(1);",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);

    try {
      await probeInstalledAdapterAuth({
        adapterId: "codex/exec-json",
        executable,
        cwd: input.invocation.cwd,
        credentialGroup: "cached-chatgpt-login",
        environment: {
          PATH: process.env.PATH ?? dirname(process.execPath),
          OPENAI_API_KEY: secret,
        },
      });
      throw new Error("auth probe unexpectedly passed");
    } catch (caught) {
      expect(caught).toMatchObject({
        code: "HARNESS_NEEDS_AUTH",
        details: {
          adapterId: "codex/exec-json",
          fallbackAttempted: false,
          readinessProbeExitCode: 1,
        },
      });
      expect((caught as { details?: Record<string, unknown> }).details).toEqual({
        adapterId: "codex/exec-json",
        fallbackAttempted: false,
        readinessProbeExitCode: 1,
      });
      expect(JSON.stringify(caught)).not.toContain(providerBody);
      expect(JSON.stringify(caught)).not.toContain(secret);
    }
  });

  test("keeps rejected version-probe output out of doctor JSON and human diagnostics", async () => {
    const input = await fixture();
    const executable = join(input.root, "codex");
    const providerStdout = "provider-version-body-must-not-appear";
    const providerStderr = "provider-version-error-must-not-appear";
    const secret = "version-probe-secret-must-not-appear";
    await writeFile(executable, [
      `#!${process.execPath}`,
      `console.log(${JSON.stringify(providerStdout)});`,
      `console.error(${JSON.stringify(`${providerStderr}:${secret}`)});`,
      "process.exit(1);",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);

    const execute = async (output: "human" | "json") => {
      let stdout = "";
      let stderr = "";
      const exit = await runCli([
        "--harness", "codex", "--transport", "exec-json", "--output", output, "cli", "doctor",
      ], {
        env: { PATH: input.root, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008884" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: (text) => { stderr += text; },
        imageBundle: sentinelImage,
      });
      return { exit, stdout, stderr };
    };

    const machine = await execute("json");
    expect(machine.exit).toBe(10);
    expect(JSON.parse(machine.stdout).problems[0]).toMatchObject({
      code: "HARNESS_INCOMPATIBLE",
      details: {
        adapterId: "codex/exec-json",
        admittedVersions: ["0.149.0-alpha.4.1"],
        repairCommand: "npm install --global @openai/codex@0.149.0-alpha.4.1",
        fallbackAttempted: false,
      },
    });
    expect(JSON.parse(machine.stdout).problems[0].details).toEqual({
      adapterId: "codex/exec-json",
      admittedVersions: ["0.149.0-alpha.4.1"],
      repairCommand: "npm install --global @openai/codex@0.149.0-alpha.4.1",
      fallbackAttempted: false,
    });
    expect(machine.stderr).toBe("");

    const human = await execute("human");
    expect(human.exit).toBe(10);
    expect(human.stdout).toContain("problem: HARNESS_INCOMPATIBLE");
    expect(human.stdout).toContain("Repair: npm install --global @openai/codex@0.149.0-alpha.4.1");
    expect(human.stderr).toBe("");
    for (const text of [machine.stdout, machine.stderr, human.stdout, human.stderr]) {
      expect(text).not.toContain(providerStdout);
      expect(text).not.toContain(providerStderr);
      expect(text).not.toContain(secret);
    }
  });

  test.skipIf(process.platform === "win32")("fails auth probing bounded when an exited command leaves pipes with a setsid descendant", async () => {
    const input = await fixture();
    const identityPath = join(input.root, "retained-auth-pipe.json");
    const executable = join(input.root, "codex");
    await writeFile(executable, [
      `#!${process.execPath}`,
      `const descendant = Bun.spawn([process.execPath, "-e", "await Bun.sleep(30_000)"], { detached: true, stdin: "ignore", stdout: "inherit", stderr: "inherit" });`,
      "descendant.unref();",
      `await Bun.write(${JSON.stringify(identityPath)}, JSON.stringify({pid: descendant.pid}));`,
      "console.log('Logged in using ChatGPT');",
      "process.exit(0);",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    let escapedPid: number | null = null;
    const started = performance.now();
    try {
      await expect(probeInstalledAdapterAuth({
        adapterId: "codex/exec-json",
        executable,
        cwd: input.root,
        credentialGroup: "cached-chatgpt-login",
        environment: { PATH: process.env.PATH ?? dirname(process.execPath) },
        timeoutMs: 500,
      })).rejects.toMatchObject({ code: "PROCESS_CLEANUP_FAILED" });
      escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      expect(pidExists(escapedPid!)).toBeTrue();
      expect(performance.now() - started).toBeLessThan(1_500);
    } finally {
      if (escapedPid === null && await Bun.file(identityPath).exists()) {
        escapedPid = JSON.parse(await readFile(identityPath, "utf8")).pid;
      }
      if (escapedPid !== null && Number.isSafeInteger(escapedPid) && escapedPid > 1 && escapedPid !== process.pid) {
        try { process.kill(escapedPid, "SIGKILL"); } catch { /* already gone */ }
      }
    }
  });
});

function pidExists(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (caught) {
    return (caught as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

describe("ordinary functional-alpha installed execution", () => {
  test.each(adapterCases)("discovers, authenticates, and completes %s without a provider call", async (adapterId) => {
    const input = await fixture();
    const [harness, transport] = adapterId.split("/") as [string, string];
    const languageArgv = ["prose", "run", "hello with spaces.prose.md", "--model", "language-owned"];
    const terminal = {
      ...terminalCarriage.terminal,
      task: { argv: languageArgv },
    };
    const assistantText = `Echoed task argv: ${JSON.stringify(languageArgv)}\n${JSON.stringify(terminal)}`;
    const observationPath = join(input.root, `${harness}-live-observation.json`);
    await writeLiveHarness(input.root, adapterId, assistantText, observationPath);
    let stdout = "";
    let stderr = "";
    const rawSecret = "raw-provider-secret-must-not-appear";
    const env: Record<string, string | undefined> = {
      PATH: input.root,
      HOME: join(input.root, "home"),
      LANG: "C.UTF-8",
      ...(adapterId === "prime/rpc" || adapterId === "omp/rpc"
        ? {
          OPENAI_API_KEY: rawSecret,
          OPENROUTER_API_KEY: rawSecret,
          ANTHROPIC_API_KEY: rawSecret,
          ANTHROPIC_OAUTH_TOKEN: rawSecret,
          GEMINI_API_KEY: rawSecret,
          GOOGLE_APPLICATION_CREDENTIALS: "/fixture/provider-key.json",
          GITHUB_TOKEN: rawSecret,
          GH_TOKEN: rawSecret,
          COPILOT_GITHUB_TOKEN: rawSecret,
          AWS_ACCESS_KEY_ID: rawSecret,
          AWS_SECRET_ACCESS_KEY: rawSecret,
          AWS_SESSION_TOKEN: rawSecret,
          AWS_PROFILE: "provider-profile",
          PRIME_AGENT_CODING_AGENT_DIR: "/fixture/prime-store-override",
          PI_CODING_AGENT_DIR: "/fixture/omp-store-override",
        }
        : {}),
    };
    const dependencies: CliDependencies = {
      env,
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008888" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
      imageBundle: sentinelImage,
    };
    const cliExit = await runCli([
      "--harness", harness,
      "--transport", transport,
      "--model", adapterId === "prime/rpc" || adapterId === "omp/rpc" ? "fixture/runner-selected-model" : "runner-selected-model",
      ...(adapterId === "prime/rpc" || adapterId === "omp/rpc"
        ? ["--auth-profile", `${harness}-harness-login`]
        : []),
      "--timeout", "2s",
      "--output", "jsonl",
      "run", "hello with spaces.prose.md", "--model", "language-owned",
    ], dependencies);
    expect(cliExit).toBe(0);
    const records = stdout.trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.map((record) => record.type)).toEqual([
      "runner.started", "harness.started", "assistant.message", "harness.completed", "runner.completed",
    ]);
    expect(records[2]).toMatchObject({
      type: "assistant.message",
      payload: { kind: "assistant.message", text: `Echoed task argv: ${JSON.stringify(languageArgv)}` },
    });
    expect(stdout).not.toContain("OPENPROSE_ECHO_TERMINAL_V0");
    const result = records.at(-1)!.payload.result;
    expect(result).toMatchObject({
      schema: "openprose.runner-result/1",
      adapter: {
        id: adapterId,
        harnessVersion: liveVersions[adapterId],
        descriptorDigestSha256: installedAdapterDefinition(adapterId).recipeSha256,
      },
      transport,
      terminal: { classification: "success", transportCompleted: true, terminalEventObserved: true, exitCode: 0 },
      semantic: { status: "not-applicable" },
      billing: { owner: "user-provider", authCategory: "harness-managed" },
      runnerExitCode: 0,
    });
    expect(result.digests.normalizedEventsSha256).toBe(
      await sha256(records.slice(0, -1).map((record) => `${canonicalJson(record)}\n`).join("")),
    );
    expect(stderr).toBe("");
    const observation = JSON.parse(await readFile(observationPath, "utf8"));
    expect(observation.argv).toContain(
      adapterId === "prime/rpc" || adapterId === "omp/rpc" ? "fixture/runner-selected-model" : "runner-selected-model",
    );
    expect(observation.argv).not.toContain("language-owned");
    expect(observation.environmentNames).not.toContain("PROSE_HARNESS");
    expect(JSON.stringify(observation)).not.toContain(rawSecret);
    expect(observation.environmentNames).not.toContain("PRIME_AGENT_CODING_AGENT_DIR");
    expect(observation.environmentNames).not.toContain("PI_CODING_AGENT_DIR");
    expect(observation.adapterControls).toEqual(
      adapterId === "prime/rpc" ? { PRIME_AGENT_TELEMETRY: "0" } : {},
    );
    if (adapterId === "prime/rpc" || adapterId === "omp/rpc") {
      expect(observation.credentialConfig).toBeNull();
      expect(result.billing).toEqual({ owner: "user-provider", authCategory: "harness-managed" });
    }
  });

  test.each([
    ["prime", "prime/rpc"],
    ["omp", "omp/rpc"],
  ] as const)("injects the selected %s credential only after its admitted version probe", async (harness, adapterId) => {
    const input = await fixture();
    const languageArgv = ["prose", "run", "credential-order.prose.md"];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    const observationPath = join(input.root, `${harness}-credential-order-execution.json`);
    const versionObservationPath = join(input.root, `${harness}-credential-order-version.json`);
    await writeLiveHarness(
      input.root,
      adapterId,
      `credential ordering\n${JSON.stringify(terminal)}`,
      observationPath,
      true,
      0,
      versionObservationPath,
    );
    const selectedSecret = `${harness}-selected-execution-secret`;
    let stdout = "";
    expect(await runCli([
      "--harness", harness,
      "--transport", "rpc",
      "--model", "fixture/model",
      "--auth-profile", "openrouter",
      "--timeout", "2s",
      "--output", "json",
      "run", "credential-order.prose.md",
    ], {
      env: {
        PATH: input.root,
        HOME: join(input.root, "cached-login-home"),
        XDG_CONFIG_HOME: join(input.root, "hostile-config"),
        HTTPS_PROXY: "https://proxy-user:proxy-password@example.invalid",
        OPENROUTER_API_KEY: selectedSecret,
        ANTHROPIC_API_KEY: "unselected-secret",
        PRIME_AGENT_TELEMETRY: "1",
        PI_CONFIG_FILES: join(input.root, "hostile-omp.yml"),
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008879" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(0);
    expect(JSON.parse(await readFile(versionObservationPath, "utf8"))).toEqual([
      "OPENPROSE_INVOCATION_ID",
      "OPENPROSE_RECURSION_TOKEN",
      "OPENPROSE_RUN_NONCE",
      "PATH",
    ]);
    const execution = JSON.parse(await readFile(observationPath, "utf8"));
    expect(execution.environmentNames).toContain("OPENROUTER_API_KEY");
    expect(execution.environmentNames).not.toContain("ANTHROPIC_API_KEY");
    expect(execution.environmentNames).toContain(
      adapterId === "prime/rpc" ? "PRIME_AGENT_CODING_AGENT_DIR" : "PI_CODING_AGENT_DIR",
    );
    expect(stdout).not.toContain(selectedSecret);
  });

  test.each([
    ["openai-api-key", "OPENAI_API_KEY"],
    ["codex-access-token", "CODEX_ACCESS_TOKEN"],
  ] as const)("accepts the explicit Codex %s route without a cached-login readiness probe", async (authProfile, credentialName) => {
    const input = await fixture();
    const executable = join(input.root, "codex");
    const authProbeMarker = join(input.root, `${authProfile}-unexpected-auth-probe`);
    await writeFile(executable, [
      `#!${process.execPath}`,
      "if (process.argv.includes('--version')) { console.log('codex-cli 0.149.0-alpha.4.1'); process.exit(0); }",
      `await Bun.write(${JSON.stringify(authProbeMarker)}, process.argv.slice(2).join(" "));`,
      "console.error('cached-login readiness must not run for an explicit credential route');",
      "process.exit(1);",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    let stdout = "";
    expect(await runCli([
      "--harness", "codex",
      "--auth-profile", authProfile,
      "--output", "json",
      "cli", "doctor",
    ], {
      env: {
        PATH: input.root,
        HOME: join(input.root, "home"),
        [credentialName]: `${authProfile}-fixture-secret`,
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008878" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(0);
    expect(JSON.parse(stdout)).toMatchObject({
      ready: true,
      selectedHarness: "codex",
      selectedAdapterId: "codex/exec-json",
      selectedAuthReadiness: "unknown",
      problems: [],
    });
    expect(await Bun.file(authProbeMarker).exists()).toBeFalse();
    expect(stdout).not.toContain(`${authProfile}-fixture-secret`);
  });

  test("redacts a selected credential echoed by an admitted adapter in every output mode and normalized evidence", async () => {
    const input = await fixture();
    const secret = "selected-machine-output-secret";
    const languageArgv = ["prose", "run", "secret-echo.prose.md"];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    await writeLiveHarness(
      input.root,
      "prime/rpc",
      `credential=${secret}\n${JSON.stringify(terminal)}`,
      join(input.root, "secret-echo-observation.json"),
      true,
      0,
      null,
      `OPENROUTER_API_KEY=${secret}`,
    );
    const execute = async (mode: "human" | "json" | "jsonl") => {
      let stdout = "";
      let stderr = "";
      const exit = await runCli([
        "--harness", "prime",
        "--transport", "rpc",
        "--model", "fixture/model",
        "--auth-profile", "openrouter",
        "--timeout", "2s",
        "--output", mode,
        "run", "secret-echo.prose.md",
      ], {
        env: {
          PATH: input.root,
          HOME: join(input.root, "home"),
          OPENROUTER_API_KEY: secret,
        },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008877" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: (text) => { stderr += text; },
        imageBundle: sentinelImage,
      });
      return { exit, stdout, stderr };
    };

    const human = await execute("human");
    expect(human.exit).toBe(0);
    expect(human.stdout).toBe("OpenProse completed; harness output was withheld by the human-output safety policy.\n");
    expect(human.stderr).toContain("OPENROUTER_API_KEY=[REDACTED]");
    const json = await execute("json");
    expect(json.exit).toBe(0);
    const jsonResult = JSON.parse(json.stdout);
    const jsonl = await execute("jsonl");
    expect(jsonl.exit).toBe(0);
    const records = jsonl.stdout.trimEnd().split("\n").map((line) => JSON.parse(line));
    const assistant = records.find((record) => record.type === "assistant.message");
    expect(assistant.payload.text).toBe("credential=[REDACTED]");
    const jsonlResult = records.at(-1)!.payload.result;
    expect(jsonResult.digests.normalizedEventsSha256).toBe(jsonlResult.digests.normalizedEventsSha256);
    expect(jsonResult.digests.normalizedEventsSha256).toBe(
      await sha256(records.slice(0, -1).map((record) => `${canonicalJson(record)}\n`).join("")),
    );
    for (const output of [human.stdout, human.stderr, json.stdout, json.stderr, jsonl.stdout, jsonl.stderr]) {
      expect(output).not.toContain(secret);
    }
    expect(json.stderr).toContain("OPENROUTER_API_KEY=[REDACTED]");
    expect(jsonl.stderr).toContain("OPENROUTER_API_KEY=[REDACTED]");
  });

  test("redacts a protected final-environment value split across machine assistant records", async () => {
    const input = await fixture();
    const homePath = "/fixture/path-split-private-home";
    const split = Math.floor(homePath.length / 2);
    const executable = join(input.root, "codex");
    const languageArgv = ["prose", "run", "split-path.prose.md"];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    await writeFile(executable, [
      `#!${process.execPath}`,
      "const argv=process.argv.slice(2);",
      "if (argv.includes('--version')) { console.log('codex-cli 0.149.0-alpha.4.1'); process.exit(0); }",
      "if (argv.join(' ')==='login status') { console.log('Logged in using ChatGPT'); process.exit(0); }",
      "console.log(JSON.stringify({type:'thread.started',thread_id:'fixture-thread'}));",
      "console.log(JSON.stringify({type:'turn.started'}));",
      `console.log(JSON.stringify({type:'item.completed',item:{type:'agent_message',text:${JSON.stringify(`home=${homePath.slice(0, split)}`)}}}));`,
      `console.log(JSON.stringify({type:'item.completed',item:{type:'agent_message',text:${JSON.stringify(homePath.slice(split))}}}));`,
      `console.log(JSON.stringify({type:'item.completed',item:{type:'agent_message',text:${JSON.stringify(JSON.stringify(terminal))}}}));`,
      "console.log(JSON.stringify({type:'turn.completed'}));",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    let stdout = "";
    expect(await runCli([
      "--harness", "codex",
      "--timeout", "2s",
      "--output", "jsonl",
      "run", "split-path.prose.md",
    ], {
      env: { PATH: input.root, HOME: homePath },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008875" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: () => {},
      imageBundle: sentinelImage,
    })).toBe(0);
    const records = stdout.trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.filter((record) => record.type === "assistant.message").map((record) => record.payload.text)).toEqual([
      "home=[REDACTED]",
      "[REDACTED]",
    ]);
    expect(stdout).not.toContain(homePath);
    expect(stdout).not.toContain(homePath.slice(0, split));
    expect(stdout).not.toContain(homePath.slice(split));
  });

  test("protects split recursion metadata in JSON, JSONL, and human output while retaining public invocation identity", async () => {
    const input = await fixture();
    const invocationId = "00000000-0000-7000-8000-000000008874";
    const recursionToken = `openprose:${invocationId}`;
    const runNonce = `adapter-probe:${invocationId}`;
    const executable = join(input.root, "codex");
    const languageArgv = ["prose", "run", "split-recursion.prose.md"];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    await writeFile(executable, [
      `#!${process.execPath}`,
      "const argv=process.argv.slice(2);",
      "if (argv.includes('--version')) { console.log('codex-cli 0.149.0-alpha.4.1'); process.exit(0); }",
      "if (argv.join(' ')==='login status') { console.log('Logged in using ChatGPT'); process.exit(0); }",
      "const recursion=process.env.OPENPROSE_RECURSION_TOKEN ?? '';",
      "const nonce=process.env.OPENPROSE_RUN_NONCE ?? '';",
      "const recursionSplit=Math.floor(recursion.length/2); const nonceSplit=Math.floor(nonce.length/2);",
      "console.log(JSON.stringify({type:'thread.started',thread_id:'fixture-thread'}));",
      "console.log(JSON.stringify({type:'turn.started'}));",
      "for (const text of [`recursion=${recursion.slice(0,recursionSplit)}`,recursion.slice(recursionSplit),`nonce=${nonce.slice(0,nonceSplit)}`,nonce.slice(nonceSplit)]) console.log(JSON.stringify({type:'item.completed',item:{type:'agent_message',text}}));",
      `console.log(JSON.stringify({type:'item.completed',item:{type:'agent_message',text:${JSON.stringify(JSON.stringify(terminal))}}}));`,
      "console.log(JSON.stringify({type:'turn.completed'}));",
    ].join("\n"), { mode: 0o700 });
    await chmod(executable, 0o700);
    const execute = async (mode: "human" | "json" | "jsonl") => {
      let stdout = "";
      expect(await runCli([
        "--harness", "codex",
        "--timeout", "2s",
        "--output", mode,
        "run", "split-recursion.prose.md",
      ], {
        env: { PATH: input.root, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => invocationId },
        writeStdout: (text) => { stdout += text; },
        writeStderr: () => {},
        imageBundle: sentinelImage,
      })).toBe(0);
      return stdout;
    };

    const jsonOutput = await execute("json");
    const jsonResult = JSON.parse(jsonOutput);
    expect(jsonResult.invocationId).toBe(invocationId);
    const jsonlOutput = await execute("jsonl");
    const records = jsonlOutput.trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.filter((record) => record.type === "assistant.message").map((record) => record.payload.text)).toEqual([
      "recursion=[REDACTED]",
      "[REDACTED]",
      "nonce=[REDACTED]",
      "[REDACTED]",
    ]);
    expect(records[0].invocationId).toBe(invocationId);
    expect(records.at(-1)!.payload.result.invocationId).toBe(invocationId);
    expect(jsonResult.digests.normalizedEventsSha256).toBe(
      records.at(-1)!.payload.result.digests.normalizedEventsSha256,
    );
    const humanOutput = await execute("human");
    for (const output of [jsonOutput, jsonlOutput, humanOutput]) {
      expect(output).not.toContain(recursionToken);
      expect(output).not.toContain(runNonce);
    }
  });

  test.each([
    ["prime", "prime/rpc"],
    ["omp", "omp/rpc"],
  ] as const)("protects the late-created %s credential config path in human and machine surfaces", async (harness, adapterId) => {
    const input = await fixture();
    const privatePathToken = "{{OPENPROSE_TEST_CREDENTIAL_CONFIG_PATH}}";
    const homePath = join(input.root, "home with session");
    const temporaryPath = join(input.root, "private tmp");
    const xdgPath = join(input.root, "private xdg config");
    const userProfilePath = join(input.root, "private windows profile");
    const sshAgentPath = join(input.root, "private ssh agent.sock");
    const languageArgv = ["prose", "run", `${harness}-private-config.prose.md`];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    const observationPath = join(input.root, `${harness}-private-config-observation.json`);
    const pathEchoes = [
      `home=${homePath}`,
      `tmp=${temporaryPath}`,
      `xdg=${xdgPath}`,
      `profile=${userProfilePath}`,
      `ssh=${sshAgentPath}`,
      `credential-config=${privatePathToken}`,
    ].join("\n");
    await writeLiveHarness(
      input.root,
      adapterId,
      `${pathEchoes}\n${JSON.stringify(terminal)}`,
      observationPath,
      true,
      0,
      null,
      pathEchoes,
    );
    const execute = async (mode: "human" | "json" | "jsonl") => {
      let stdout = "";
      let stderr = "";
      const exit = await runCli([
        "--harness", harness,
        "--transport", "rpc",
        "--model", "fixture/model",
        "--auth-profile", "openrouter",
        "--timeout", "2s",
        "--output", mode,
        "run", `${harness}-private-config.prose.md`,
      ], {
        env: {
          PATH: input.root,
          HOME: homePath,
          TMPDIR: temporaryPath,
          XDG_CONFIG_HOME: xdgPath,
          USERPROFILE: userProfilePath,
          SSH_AUTH_SOCK: sshAgentPath,
          OPENROUTER_API_KEY: `${harness}-private-config-selected-secret`,
        },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008876" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: (text) => { stderr += text; },
        imageBundle: sentinelImage,
      });
      return { exit, stdout, stderr };
    };

    const human = await execute("human");
    const humanObservation = JSON.parse(await readFile(observationPath, "utf8"));
    const privateConfigPath = humanObservation.credentialConfig.path as string;
    expect(privateConfigPath).toContain(
      adapterId === "prime/rpc" ? "openprose-prime-" : "openprose-transport-",
    );
    expect(human.exit).toBe(0);
    expect([
      ["home", "tmp", "xdg", "profile", "ssh", "credential-config"]
        .map((name) => `${name}=[REDACTED]`)
        .join("\n") + "\n",
      "OpenProse completed; harness output was withheld by the human-output safety policy.\n",
    ]).toContain(human.stdout);
    expect(human.stderr).toContain("credential-config=[REDACTED]");
    const json = await execute("json");
    expect(json.exit).toBe(0);
    const jsonResult = JSON.parse(json.stdout);
    const jsonl = await execute("jsonl");
    expect(jsonl.exit).toBe(0);
    const records = jsonl.stdout.trimEnd().split("\n").map((line) => JSON.parse(line));
    expect(records.find((record) => record.type === "assistant.message").payload.text).toBe(
      ["home", "tmp", "xdg", "profile", "ssh", "credential-config"]
        .map((name) => `${name}=[REDACTED]`)
        .join("\n"),
    );
    expect(jsonResult.digests.normalizedEventsSha256).toBe(
      records.at(-1)!.payload.result.digests.normalizedEventsSha256,
    );
    for (const output of [human.stdout, human.stderr, json.stdout, json.stderr, jsonl.stdout, jsonl.stderr]) {
      for (const protectedPath of [homePath, temporaryPath, xdgPath, userProfilePath, sshAgentPath, privateConfigPath]) {
        expect(output).not.toContain(protectedPath);
      }
      expect(output).not.toContain("openprose-transport-");
      expect(output).not.toContain("openprose-prime-");
    }
    expect(json.stderr).toContain("home=[REDACTED]");
    expect(json.stderr).toContain("credential-config=[REDACTED]");
    expect(jsonl.stderr).toContain("home=[REDACTED]");
    expect(jsonl.stderr).toContain("credential-config=[REDACTED]");
  });

  test("dry-run and doctor perform discovery, version, and auth readiness without launching a model", async () => {
    const input = await fixture();
    const observationPath = join(input.root, "must-not-launch.json");
    await writeLiveHarness(
      input.root,
      "codex/exec-json",
      terminalCarriage.streams["codex/exec-json"].assistantText,
      observationPath,
    );
    const run = async (args: string[]) => {
      let stdout = "";
      const exit = await runCli(args, {
        env: { PATH: input.root, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008887" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: () => {},
        imageBundle: sentinelImage,
      });
      return { exit, report: JSON.parse(stdout) };
    };
    const dry = await run(["--harness", "codex", "--dry-run", "--output", "json", "run"]);
    expect(dry).toMatchObject({
      exit: 0,
      report: {
        readiness: "ready",
        selection: { adapterId: "codex/exec-json", runtimeVersion: liveVersions["codex/exec-json"] },
        auth: { readiness: "ready" },
        wouldStartModel: false,
      },
    });
    const doctor = await run(["--harness", "codex", "--output", "json", "cli", "doctor"]);
    expect(doctor).toMatchObject({
      exit: 0,
      report: { ready: true, selectedHarnessVersion: liveVersions["codex/exec-json"], problems: [] },
    });
    expect(await Bun.file(observationPath).exists()).toBeFalse();
  });

  test.each([
    ["prime", "prime/rpc", "prime-harness-login"],
    ["prime", "prime/rpc", "openrouter"],
    ["omp", "omp/rpc", "omp-harness-login"],
    ["omp", "omp/rpc", "openrouter"],
  ] as const)("keeps %s preflight model-free with unknown auth for profile %s", async (harness, adapterId, authProfile) => {
    const input = await fixture();
    const observationPath = join(input.root, `${harness}-${authProfile}-must-not-launch.json`);
    await writePreflightOnlyHarness(input.root, adapterId, observationPath);
    for (const operation of ["dry-run", "doctor"] as const) {
      const execute = async (output: "human" | "json") => {
        let stdout = "";
        const exit = await runCli([
          "--harness", harness,
          "--transport", "rpc",
          "--model", "fixture/model",
          "--auth-profile", authProfile,
          "--output", output,
          ...(operation === "dry-run" ? ["--dry-run", "run", "hello.prose.md"] : ["cli", "doctor"]),
        ], {
          env: {
            PATH: input.root,
            HOME: join(input.root, "home"),
            OPENROUTER_API_KEY: "fixture-key",
            PRIME_AGENT_CODING_AGENT_DIR: "/fixture/prime-store-override",
            PI_CODING_AGENT_DIR: "/fixture/omp-store-override",
          },
          processCwd: input.invocation.cwd,
          userConfigPath: join(input.root, "absent.toml"),
          clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
          ids: { invocationId: () => "00000000-0000-7000-8000-000000008886" },
          writeStdout: (text) => { stdout += text; },
          writeStderr: () => {},
          imageBundle: sentinelImage,
        });
        return { exit, stdout };
      };
      const machine = await execute("json");
      expect(machine.exit).toBe(0);
      const report = JSON.parse(machine.stdout);
      if (operation === "dry-run") expect(report.auth.readiness).toBe("unknown");
      else expect(report.selectedAuthReadiness).toBe("unknown");
      const human = await execute("human");
      expect(human.exit).toBe(0);
      expect(human.stdout).toContain(operation === "dry-run"
        ? "Readiness: mechanically ready; authentication unverified\n"
        : "status: mechanically ready; authentication unverified\n");
      expect(await Bun.file(observationPath).exists()).toBeFalse();
      expect(await Bun.file(`${observationPath}.auth-probe`).exists()).toBeFalse();
      expect(await Bun.file(`${observationPath}.runtime-launch`).exists()).toBeFalse();
    }
  }, 30_000);

  test("keeps ordinary human readiness for verified and not-applicable auth, and blocked wording for failures", async () => {
    const input = await fixture();
    await writeLiveHarness(
      input.root,
      "codex/exec-json",
      terminalCarriage.streams["codex/exec-json"].assistantText,
      join(input.root, "human-readiness-must-not-launch.json"),
    );
    const execute = async (args: string[], path: string, imageBundle = sentinelImage) => {
      let stdout = "";
      const exit = await runCli(args, {
        env: { PATH: path, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008885" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: () => {},
        imageBundle,
      });
      return { exit, stdout };
    };
    for (const [args, line] of [
      [["--harness", "codex", "--output", "human", "cli", "doctor"], "status: ready\n"],
      [["--harness", "codex", "--dry-run", "--output", "human", "run"], "Readiness: ready\n"],
    ] as const) {
      const verified = await execute([...args], input.root);
      expect(verified.exit).toBe(0);
      expect(verified.stdout).toContain(line);
      expect(verified.stdout).not.toContain("mechanically ready; authentication unverified");
    }
    for (const [args, line, authLine] of [
      [["--harness", "mock", "--output", "human", "cli", "doctor"], "status: ready\n", "auth readiness: not-applicable\n"],
      [["--harness", "mock", "--dry-run", "--output", "human", "run"], "Readiness: ready\n", "Auth: none-test-only (not-applicable)\n"],
    ] as const) {
      const notApplicable = await execute([...args], input.root, sentinelFixtureImage);
      expect(notApplicable.exit).toBe(0);
      expect(notApplicable.stdout).toContain(line);
      expect(notApplicable.stdout).toContain(authLine);
      expect(notApplicable.stdout).not.toContain("mechanically ready; authentication unverified");
    }
    for (const [args, line] of [
      [["--harness", "codex", "--output", "human", "cli", "doctor"], "status: not ready\n"],
      [["--harness", "codex", "--dry-run", "--output", "human", "run"], "Readiness: blocked\n"],
    ] as const) {
      const blocked = await execute([...args], join(input.root, "empty-path"));
      expect(blocked.exit).toBe(10);
      expect(blocked.stdout).toContain(line);
      expect(blocked.stdout).not.toContain("mechanically ready; authentication unverified");
    }
  });

  test("human mode prints the placeholder's visible assistant text but not its control terminal", async () => {
    const input = await fixture();
    const languageArgv = ["prose", "write", "hello"];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    const visible = `Echoed task argv: ${JSON.stringify(languageArgv)}`;
    await writeLiveHarness(
      input.root,
      "codex/exec-json",
      `${visible}\n${JSON.stringify(terminal)}`,
      join(input.root, "human-observation.json"),
    );
    let stdout = "";
    let stderr = "";
    const exit = await runCli(["--harness", "codex", "write", "hello"], {
      env: { PATH: input.root, HOME: join(input.root, "home") },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008885" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
      imageBundle: sentinelImage,
    });
    expect(exit).toBe(0);
    expect(stdout).toBe(
      "OpenProse completed; harness output was withheld by the human-output safety policy.\n",
    );
    expect(stdout).not.toContain("OPENPROSE_ECHO_TERMINAL_V0");
    expect(stderr).toBe("");
  });

  test.each(adapterCases)("human %s output arrives before terminal settlement without exposing the carrier", async (adapterId) => {
    const input = await fixture();
    const [harness, transport] = adapterId.split("/") as [string, string];
    const languageArgv = ["prose", "run", "streaming.prose.md"];
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    const visible = `Streaming assistant output from ${harness}.`;
    await writeLiveHarness(
      input.root,
      adapterId,
      `${visible}\n${JSON.stringify(terminal)}`,
      join(input.root, `${harness}-stream-observation.json`),
      true,
      300,
    );
    let stdout = "";
    let stderr = "";
    let settled = false;
    const run = runCli([
      "--harness", harness,
      "--transport", transport,
      "--model", adapterId === "prime/rpc" || adapterId === "omp/rpc" ? "fixture/runner-selected-model" : "runner-selected-model",
      ...(adapterId === "prime/rpc" || adapterId === "omp/rpc"
        ? ["--auth-profile", "openrouter"]
        : []),
      "--timeout", "2s",
      "run", "streaming.prose.md",
    ], {
      env: {
        PATH: input.root,
        HOME: join(input.root, "home"),
        ...(adapterId === "prime/rpc" || adapterId === "omp/rpc"
          ? { OPENROUTER_API_KEY: "streaming-secret-must-not-appear" }
          : {}),
      },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008883" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
      imageBundle: sentinelImage,
    }).finally(() => { settled = true; });

    const deadline = performance.now() + 1_500;
    while (!stdout.includes(visible) && !settled && performance.now() < deadline) await Bun.sleep(5);
    expect(stdout).toBe(`${visible}\n`);
    expect(settled).toBeFalse();
    expect(stdout).not.toContain("OPENPROSE_ECHO_TERMINAL_V0");
    expect(stdout).not.toContain("streaming-secret-must-not-appear");

    expect(await run).toBe(0);
    expect(stdout).toBe(`${visible}\n`);
    expect(stderr).toBe("");
  });

  test("human streaming withholds an invalid terminal and all task, model, control, and credential literals", async () => {
    const input = await fixture();
    const languageArgv = ["prose", "run", "protected.prose.md"];
    const deliveredTask = canonicalJson({
      schema: input.image.manifest.taskEnvelope.schemaId,
      argv: languageArgv,
      interactionMode: "non-interactive",
    });
    const secret = "assistant-secret-must-not-appear";
    const model = "runner-selected-model";
    const rawTerminal = '{"marker":"raw-control-must-not-appear"}';
    await writeLiveHarness(
      input.root,
      "codex/exec-json",
      `Safe assistant text containing ${secret}\nprotected.prose.md\n${model}\n${deliveredTask}\n${rawTerminal}`,
      join(input.root, "protected-stream-observation.json"),
    );
    let stdout = "";
    let stderr = "";
    const exit = await runCli(["--harness", "codex", "--model", model, "run", "protected.prose.md"], {
      env: { PATH: input.root, HOME: join(input.root, "home"), OPENAI_API_KEY: secret },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008882" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
      imageBundle: sentinelImage,
    });
    expect(exit).toBe(22);
    expect(stdout).toBe("");
    expect(stderr).toContain("PROTOCOL_MALFORMED");
    for (const protectedText of [
      secret,
      "protected.prose.md",
      model,
      deliveredTask,
      rawTerminal,
      "raw-control-must-not-appear",
    ]) {
      expect(`${stdout}\n${stderr}`).not.toContain(protectedText);
    }
  });

  test("successful human streaming replaces protected settled output with a safe notice", async () => {
    const input = await fixture();
    const languageArgv = ["prose", "run", "protected-success.prose.md"];
    const model = "runner-selected-model";
    const terminal = { ...terminalCarriage.terminal, task: { argv: languageArgv } };
    const control = '{"schema":"unsettled-control"}';
    await writeLiveHarness(
      input.root,
      "codex/exec-json",
      `Attempted ${model} and protected-success.prose.md\n${control}\n${JSON.stringify(terminal)}`,
      join(input.root, "protected-success-stream-observation.json"),
    );
    let stdout = "";
    let stderr = "";
    const exit = await runCli([
      "--harness", "codex", "--model", model, "run", "protected-success.prose.md",
    ], {
      env: { PATH: input.root, HOME: join(input.root, "home") },
      processCwd: input.invocation.cwd,
      userConfigPath: join(input.root, "absent.toml"),
      clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
      ids: { invocationId: () => "00000000-0000-7000-8000-000000008881" },
      writeStdout: (text) => { stdout += text; },
      writeStderr: (text) => { stderr += text; },
      imageBundle: sentinelImage,
    });
    expect(exit).toBe(0);
    expect(stdout).toBe(
      "OpenProse completed; harness output was withheld by the human-output safety policy.\n",
    );
    expect(stderr).toBe("");
    for (const protectedText of [model, "protected-success.prose.md", control, "unsettled-control"]) {
      expect(stdout).not.toContain(protectedText);
    }
  });

  test("fails closed on missing auth and on a missing image terminal without fallback", async () => {
    const input = await fixture();
    const observationPath = join(input.root, "closed-failure-observation.json");
    await writeLiveHarness(input.root, "codex/exec-json", "no terminal", observationPath, false);
    const execute = async () => {
      let stdout = "";
      const exit = await runCli(["--harness", "codex", "--output", "json", "run", "hello.prose.md"], {
        env: { PATH: input.root, HOME: join(input.root, "home") },
        processCwd: input.invocation.cwd,
        userConfigPath: join(input.root, "absent.toml"),
        clock: { now: () => "2026-01-01T00:00:00.000Z", monotonicMs: () => 300 },
        ids: { invocationId: () => "00000000-0000-7000-8000-000000008886" },
        writeStdout: (text) => { stdout += text; },
        writeStderr: () => {},
        imageBundle: sentinelImage,
      });
      return { exit, result: JSON.parse(stdout) };
    };
    expect(await execute()).toMatchObject({
      exit: 10,
      result: { error: { code: "HARNESS_NEEDS_AUTH" }, runnerExitCode: 10 },
    });
    expect(await Bun.file(observationPath).exists()).toBeFalse();

    await writeLiveHarness(input.root, "codex/exec-json", "no terminal", observationPath, true);
    expect(await execute()).toMatchObject({
      exit: 22,
      result: {
        error: { code: "PROTOCOL_MALFORMED" },
        semantic: { status: "unknown", terminalEnvelopeDigestSha256: null },
        runnerExitCode: 22,
      },
    });
    expect(await Bun.file(observationPath).exists()).toBeTrue();
  });
});

test("the product oracle base environment remains the shared oracle list", () => {
  expect(oracle.baseEnvironmentAllowlist).toContain("PATH");
  expect(oracle.environmentRules.alwaysStrip).toEqual(["OPENPROSE_TOKEN", "PROSE_TOKEN", "PROSE_BILLING_TOKEN"]);
});

describe("image-declared terminal recovery", () => {
  test("accepts only the final exact envelope declared by the verified image", async () => {
    const input = await fixture();
    const properties = imageTerminalSchema(input.image).properties as Record<string, { const?: unknown }>;
    const terminal = {
      schema: properties.schema!.const,
      semanticStatus: properties.semanticStatus!.const,
      placeholder: properties.placeholder!.const,
      marker: properties.marker!.const,
      task: { argv: input.invocation.task.argv },
    };
    expect(recoverImageTerminalEnvelope(input.image, [
      { type: "session.started" },
      { type: "assistant.message", text: `echoed task\n${JSON.stringify(terminal)}\n` },
      { type: "session.completed" },
    ], input.invocation)).toEqual(terminal);
  });

  test("accepts a closed image terminal without task while rejecting undeclared fields", async () => {
    const input = await fixture();
    const image = await verifyRuntimeImage(sentinelFixtureImage);
    const terminal = {
      schema: "openprose.sentinel-terminal-envelope/1",
      semanticStatus: "not-applicable",
      marker: "OPENPROSE_SENTINEL_TERMINAL_V1",
    };
    expect(recoverImageTerminalEnvelope(image, [
      { type: "assistant.message", text: `transport complete\n${JSON.stringify(terminal)}` },
    ], input.invocation)).toEqual(terminal);
    expect(() => recoverImageTerminalEnvelope(image, [
      { type: "assistant.message", text: JSON.stringify({ ...terminal, task: { argv: input.invocation.task.argv } }) },
    ], input.invocation)).toThrow(expect.objectContaining({ code: "PROTOCOL_MALFORMED" }));

    const optionalTaskSchema = structuredClone(imageTerminalSchema(image));
    (optionalTaskSchema.properties as Record<string, unknown>).task = {
      type: "object",
      additionalProperties: false,
      required: ["argv"],
      properties: { argv: { type: "array", items: { type: "string" } } },
    };
    const optionalTaskImage = {
      ...image,
      files: new Map(image.files).set(
        image.manifest.terminalEnvelope.path,
        new TextEncoder().encode(JSON.stringify(optionalTaskSchema)),
      ),
    };
    expect(recoverImageTerminalEnvelope(optionalTaskImage, [
      { type: "assistant.message", text: JSON.stringify(terminal) },
    ], input.invocation)).toEqual(terminal);
    const optionalTask = { ...terminal, task: { argv: ["different"] } };
    expect(recoverImageTerminalEnvelope(optionalTaskImage, [
      { type: "assistant.message", text: JSON.stringify(optionalTask) },
    ], input.invocation)).toEqual(optionalTask);
  });

  test("echo terminals still require and exactly bind the runner-owned task argv", async () => {
    const input = await fixture();
    const properties = imageTerminalSchema(input.image).properties as Record<string, { const?: unknown }>;
    const valid = {
      schema: properties.schema!.const,
      semanticStatus: properties.semanticStatus!.const,
      placeholder: properties.placeholder!.const,
      marker: properties.marker!.const,
      task: { argv: input.invocation.task.argv },
    };
    const { task: _task, ...absentTask } = valid;
    for (const terminal of [absentTask, { ...valid, task: { argv: ["wrong"] } }]) {
      expect(() => recoverImageTerminalEnvelope(input.image, [
        { type: "assistant.message", text: JSON.stringify(terminal) },
      ], input.invocation)).toThrow(expect.objectContaining({ code: "PROTOCOL_MALFORMED" }));
    }
    expect(() => recoverImageTerminalEnvelope(input.image, [
      { type: "session.started" },
      { type: "assistant.message", text: JSON.stringify({ ...valid, marker: "forged" }) },
      { type: "session.completed" },
    ], input.invocation)).toThrow(expect.objectContaining({ code: "PROTOCOL_MALFORMED" }));
  });
});
