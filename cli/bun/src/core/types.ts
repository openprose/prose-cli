export const RUNNER_NAME = "bun";

export type OutputMode = "human" | "json" | "jsonl";
export type SourceKind = "default" | "user-config" | "project-config" | "environment" | "flag";

export interface ValueSource {
  kind: SourceKind;
  location: string;
}

export interface GlobalFlags {
  harness?: string;
  transport?: string;
  cwd?: string;
  model?: string;
  authProfile?: string;
  nativeProfile?: string;
  nativeMaxTurns?: string;
  nativeTimeout?: string;
  nativeToolTimeout?: string;
  nativeOutputBytes?: string;
  nativeAddDirs?: string[];
  nativeAllowTools?: string[];
  nativeLog?: string;
  outputContract?: string;
  permissionMode?: string;
  timeout?: string;
  output?: OutputMode;
  dryRun?: boolean;
  color?: boolean;
  verbose?: boolean;
}

export interface EffectiveValues {
  harness: string;
  transport: string;
  model: string | null;
  timeout: string;
  output: OutputMode;
  color: boolean;
  verbose: boolean;
  authProfile: string | null;
  nativeProfile?: string;
  nativeMaxTurns?: string;
  nativeTimeout?: string;
  nativeToolTimeout?: string;
  nativeOutputBytes?: string;
  nativeAddDirs?: string[];
  nativeAllowTools?: string[];
  nativeLog?: string;
  outputContract?: string;
  permissionMode?: string | null;
}

export interface EffectiveConfiguration {
  cwd: string;
  cwdSource: ValueSource;
  values: EffectiveValues;
  sources: { [K in keyof EffectiveValues]: ValueSource };
  projectConfigPath: string | null;
  userConfigPath: string;
}

export type RunnerOperation =
  | "package"
  | "doctor"
  | "harness-list"
  | "harness-use"
  | "prime-cleanup"
  | "config-explain"
  | "auth-status"
  | "auth-login"
  | "auth-logout"
  | "org-list";

export type ParsedEntrypoint =
  | { kind: "weave"; global: GlobalFlags; argv: string[] }
  | { kind: "help"; global: GlobalFlags }
  | { kind: "version"; global: GlobalFlags }
  | { kind: "operation"; global: GlobalFlags; operation: RunnerOperation; json: boolean; value?: string; packageCommand?: import("./package-args").PackageCommand }
  | { kind: "service"; global: GlobalFlags; command: import("./service/manifest").ServiceCommand }
  /** `redirect`: the words also name a service command; rejected unless an operand exists on disk, else a HOSTED_UNAVAILABLE hint. */
  | { kind: "language"; global: GlobalFlags; argv: string[]; redirect?: import("./service/manifest").CliRedirect };

export interface TaskEnvelope {
  schema: string;
  argv: string[];
  interactionMode: "non-interactive";
}

export interface RunnerInvocation {
  nativeConfiguration?: Record<string,unknown>;
  nativeLimits?: Record<string,number>;
  nativeOutputLimits?: {maxAggregateStdoutBytes:number;maxNativeCaptureBytes:number;captureEnabled:boolean};
  schema: "openprose.runner-invocation/1";
  invocationId: string;
  cwd: string;
  languageImage: {
    formatVersion: "openprose.skill-runtime-image/1";
    version: string;
    sha256: string;
  };
  runner: { name: "bun"; version: string; commit: string };
  harness: string;
  transport: string;
  recursionToken: string;
  task: TaskEnvelope;
  taskDigestSha256: string;
}

export type RunnerErrorCode =
  | "CONFIG_INVALID"
  | "INVOCATION_INVALID"
  | "HARNESS_UNAVAILABLE"
  | "HARNESS_INCOMPATIBLE"
  | "HARNESS_NEEDS_AUTH"
  | "TRANSPORT_UNSUPPORTED"
  | "PROMPT_CHANNEL_UNSUPPORTED"
  | "IMAGE_INVALID"
  | "IMAGE_TOO_LARGE"
  | "RECURSIVE_INVOCATION"
  | "STARTUP_TIMEOUT"
  | "PROTOCOL_MALFORMED"
  | "PROTOCOL_TRUNCATED"
  | "HARNESS_FAILED"
  | "SEMANTIC_STATUS_UNKNOWN"
  | "CANCELLED"
  | "PROCESS_CLEANUP_FAILED"
  | "HOSTED_UNAVAILABLE"
  | "HOSTED_AUTH_REQUIRED"
  | "HOSTED_QUOTA_EXCEEDED"
  | "SERVICE_UNAVAILABLE"
  | "SERVICE_AUTH_REQUIRED"
  | "SERVICE_PROTOCOL_INVALID"
  | "CREDENTIAL_STORE_UNAVAILABLE"
  | "DEVICE_AUTH_FAILED"
  | "DEVICE_AUTH_EXPIRED"
  | "CONFIRMATION_REQUIRED"
  | "SERVICE_REQUEST_REJECTED"
  | "SERVICE_RESOURCE_NOT_FOUND"
  | "SERVICE_FEATURE_DISABLED"
  | "SERVICE_BALANCE_INSUFFICIENT"
  | "SERVICE_PREMIUM_MODEL_LOCKED"
  | "SERVICE_ACCOUNT_SUSPENDED"
  | "SERVICE_WRITE_CONFLICT"
  | "GITHUB_LINK_REQUIRED"
  | "SERVICE_RESPONSE_TOO_LARGE"
  | "SERVICE_WATCH_DEADLINE"
  | "HOSTED_RUN_FAILED"
  | "RUN_SUBMISSION_AMBIGUOUS"
  | "HOSTED_RUN_DETACHED"
  | "HOSTED_RUN_CANCELLED"
  | "EXAMPLE_NOT_VIEWABLE"
  | "INTERNAL_ERROR";

export type RunnerBoundary =
  | "invocation"
  | "configuration"
  | "image"
  | "adapter"
  | "authentication"
  | "hosted-service"
  | "process"
  | "protocol"
  | "semantic-terminal"
  | "cleanup"
  | "runner";

export interface RunnerErrorShape {
  schema: "openprose.runner-error/1";
  code: RunnerErrorCode;
  boundary: RunnerBoundary;
  message: string;
  action: string;
  exitCode: number;
  retryable: boolean;
  details?: Record<string, unknown>;
}

export interface RuntimePrerequisiteObservation {
  runtime: "bun";
  versionRange: ">=1.3.14";
  detectedVersion: string | null;
  availability: "available" | "missing" | "incompatible";
  repairCommand: "npm install --global bun@1.3.14 @oh-my-pi/pi-coding-agent@18.0.9";
}

export class RunnerFailure extends Error {
  readonly code: RunnerErrorCode;
  readonly boundary: RunnerBoundary;
  readonly action: string;
  readonly exitCode: number;
  readonly retryable: boolean;
  readonly details?: Record<string, unknown>;

  constructor(input: Omit<RunnerErrorShape, "schema">) {
    super(input.message);
    this.name = "RunnerFailure";
    this.code = input.code;
    this.boundary = input.boundary;
    this.action = input.action;
    this.exitCode = input.exitCode;
    this.retryable = input.retryable;
    if (input.details !== undefined) this.details = input.details;
  }

  toJSON(): RunnerErrorShape {
    const shape: RunnerErrorShape = {
      schema: "openprose.runner-error/1",
      code: this.code,
      boundary: this.boundary,
      message: this.message,
      action: this.action,
      exitCode: this.exitCode,
      retryable: this.retryable,
    };
    if (this.details !== undefined) shape.details = this.details;
    return shape;
  }
}

export interface ImageManifestEntry {
  path: string;
  sha256: string;
  mediaType: "text/markdown; charset=utf-8";
  byteLength: number;
}

export interface ExternalImageArtifact {
  schemaId?: string;
  id?: string;
  path: string;
  sha256: string;
}

export interface RuntimeImageManifest {
  schema: "openprose.skill-runtime-image-manifest/1";
  imageFormatVersion: "openprose.skill-runtime-image/1";
  imageVersion: string;
  languageVersion: string;
  skillVersion: string;
  runtimeContractVersion: string;
  semanticSourceRevision: string;
  purpose: "canonical-language-runtime" | "functional-alpha-placeholder" | "sentinel-transport-test";
  releaseEligible: boolean;
  normalization: {
    encoding: "utf-8";
    newlines: "lf";
    byteOrderMark: "forbidden";
    pathSeparator: "/";
  };
  payload: ImageManifestEntry[];
  modelVisibleBytes: {
    serialization: "ordered-raw-concatenation-v1";
    byteLength: number;
    sha256: string;
  };
  aggregateSha256: { algorithm: "sha256-path-length-nul-v1"; sha256: string };
  instructionPlacements: Array<{
    id: string;
    strictness: "strict" | "degraded";
    preservesHarnessBasePrompt: boolean;
  }>;
  taskEnvelope: ExternalImageArtifact & { schemaId: string };
  oneFieldFraming: ExternalImageArtifact & { id: string };
  terminalEnvelope: ExternalImageArtifact & { schemaId: string };
  minimumTransportRequirements: {
    nonInteractive: "required";
    structuredOutput: "required";
    ambientIsolation: "required";
    terminalEnvelope: "required";
    boundedStreaming: "required";
  };
}

export interface RuntimeImageBundle {
  manifest: RuntimeImageManifest;
  files: ReadonlyMap<string, Uint8Array>;
}

export interface VerifiedRuntimeImage extends RuntimeImageBundle {
  aggregateSha256: string;
  modelVisibleBytesSha256: string;
}
