import type { RunnerInvocation, RuntimePrerequisiteObservation, VerifiedRuntimeImage } from "../core/types";
import type { ProcessSupervisionResult, RawTransportEvent } from "../supervision/types";

export type InstalledAdapterId =
  | "agents-sdk/jsonl"
  | "codex/exec-json"
  | "claude/print-stream-json"
  | "prime/rpc"
  | "omp/rpc";

export type RecipeValue =
  | "executable"
  | "image-path"
  | "image-utf8"
  | "image-developer-config"
  | "image-base-config"
  | "rendered-config-path"
  | "task-json"
  | "task-path"
  | "cwd"
  | "model"
  | "invocation-id"
  | "daemon-socket-path";

export type RecipeArgvToken = { literal: string } | { value: RecipeValue };

export interface RecipeOptionalArgv {
  when: "model-present";
  placement: "append" | "before-final-argument" | "before-final-pair";
  argv: RecipeArgvToken[];
}

export interface InstalledAdapterRecipe {
  schema: "openprose.adapter-admission-recipe/1";
  recipeVersion: string;
  adapterId: InstalledAdapterId;
  state: "frozen";
  runtime: "installed-process";
  identity: {
    kind: "executable";
    executableNames: string[];
    packageIdentity: string | null;
  };
  support: {
    /** Human-readable compatibility summary. Admission uses admittedVersions and minimumVersion only. */
    versionRange: string;
    /** Exact audited versions. */
    admittedVersions: string[];
    /** Optional floor: any stable release at or above it within the same major version is admitted. */
    minimumVersion?: string;
    repairCommand: string;
    runtimePrerequisites?: RuntimePrerequisiteRequirement[];
    platforms: string[];
  };
  probe: {
    argv: RecipeArgvToken[];
    timeoutMs: number;
    versionPattern: string;
    versionStream: "stdout" | "stderr";
  };
  launch: {
    argv: RecipeArgvToken[];
    optionalArgv: RecipeOptionalArgv[];
    interactionMode: "non-interactive";
    shell: false;
    outerPty: false;
    stdin: "closed" | "structured-request" | "protocol";
    stdinLifecycle: "close-after-write" | "close-after-terminal-event";
    stdoutProtocol: "json" | "jsonl" | "json-rpc" | "structured-sdk";
    stderr: "diagnostics-only";
    instructionPlacement: {
      manifestPlacementId: string;
      strictness: "strict" | "degraded";
      preservesHarnessBasePrompt: boolean;
    };
    imageDelivery: { mechanism: string; field: string | null; encoding: string };
    taskDelivery: { mechanism: string; field: string | null; encoding: string };
    controls?: {
      ownedConfigOverlay: {
        argvFlag: string;
        value: "rendered-config-path";
        encoding: "utf8";
        mode: "0600";
        bytes: string;
        byteLength: number;
        sha256: string;
        precedence: "final-cli-overlay";
      };
      protocolPrelude: {
        barrierEvent: string;
        stateRequestType: string;
        stateRequestIdSuffix: string;
        promptRequestIdSuffix: string;
        requiredStateDataArrayField: string;
      };
    };
  };
  isolation: { guarantee: "enforced" | "advisory" | "unsupported"; ambientDisabled: string[]; preserved: string[] };
  cancellation: {
    protocol: "documented-protocol" | "signal" | "api" | "none";
    containment: "unsupported";
    graceMs: number;
    hardKillAfterMs: number;
  };
  auth: { category: "harness-managed"; readinessProbe: string; secretsInArgv: false };
  billingOwner: "user-provider";
  requiredTerminal: { event: string; acceptableExitCodes: number[]; eofWithoutEvent: "protocol-error" };
  admissionClaims: string[];
}

export interface InstalledAdapterDefinition {
  id: InstalledAdapterId;
  recipe: InstalledAdapterRecipe;
  /** SHA-256 of the exact frozen shared recipe JSON bytes. */
  recipeSha256: string;
  runtimePrerequisites: readonly RuntimePrerequisiteRequirement[];
  credentialGroups: Readonly<Record<string, readonly string[]>>;
  credentialRequirements: Readonly<Record<string, CredentialRequirement>>;
  strictAdmission: "blocked";
  billingOwner: "user-provider";
  authCategory: "harness-managed";
}

export interface RuntimePrerequisiteRequirement {
  runtime: "bun";
  versionRange: ">=1.3.14";
  repairCommand: RuntimePrerequisiteObservation["repairCommand"];
}

export type CredentialRequirement =
  | { kind: "probe-owned" }
  | { kind: "any-nonempty"; alternatives: readonly (readonly string[])[] };

export interface InstalledLaunchPlan {
  adapterId: InstalledAdapterId;
  executable: string;
  argv: string[];
  stdinBytes: Uint8Array | null;
  stdinLifecycle: "close-after-write" | "close-after-terminal-event";
  imagePath: string;
  taskPath: string;
  daemonSocketPath: string | null;
  imageSha256: string;
  taskSha256: string;
  renderedPayloadSha256: string | null;
  credentialGroup: string;
  shell: false;
  outerPty: false;
  interactionMode: "non-interactive";
  billingOwner: "user-provider";
  authCategory: "harness-managed";
  admissionStatus: "blocked";
}

export interface InstalledAdapterOptions {
  outputContract?: string;
  nativeLog?: string;
  adapterId: InstalledAdapterId;
  executable: string;
  harnessVersion?: string;
  credentialGroup: string;
  invocation: RunnerInvocation;
  image: VerifiedRuntimeImage;
  ambient: Readonly<Record<string, string | undefined>>;
  timeoutMs: number;
  model?: string | null;
  nativeProfile?: string;
  nativeMaxTurns?: string;
  nativeTimeout?: string;
  nativeToolTimeout?: string;
  nativeOutputBytes?: string;
  nativeAddDirs?: string[];
  nativeAllowTools?: string[];
  permissionMode?: string | null;
  fixtureInterpreter?: string;
  wrapperExecutable?: string;
  platform?: NodeJS.Platform;
  arch?: NodeJS.Architecture;
  cancelSignal?: AbortSignal;
  /**
   * Creates a human-mode sink only after the final launch environment exists,
   * so late-created private paths participate in streaming protection.
   */
  createAssistantMessageSink?(
    protectedValues: readonly string[],
  ): (text: string) => void | Promise<void>;
  /** Provider-free test-only controls. Ordinary execution never sets these. */
  observationPath?: string;
  /** Provider-free test-only private-root injection. Ordinary execution never sets this. */
  temporaryRoot?: string;
  /** Compiled-out per-invocation synchronization for provider-free source tests. */
  testHooks?: {
    afterTransportDirectoryCreated?(directory: string): void | Promise<void>;
  };
}

export interface InstalledAdapterResult {
  sdkObservations?: import("./sdk-observation").SdkObservations;
  nativeConfiguration?: Record<string,unknown> | undefined;
  plan: InstalledLaunchPlan;
  process: ProcessSupervisionResult;
  /** Public projection; raw process events remain private terminal-recovery input. */
  publicEvents: RawTransportEvent[];
  publicStderr: string;
}

export type ProviderFreeAdapterOptions = InstalledAdapterOptions;
export type ProviderFreeAdapterResult = InstalledAdapterResult;
