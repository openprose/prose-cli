declare const OPENPROSE_BUILD_COMMIT: string | undefined;
declare const OPENPROSE_BUILD_VERSION: string | undefined;
declare const OPENPROSE_BUILD_PROFILE: "development" | "release" | undefined;
declare const OPENPROSE_TEST_SEAMS: boolean | undefined;
declare const OPENPROSE_WINDOWS_HOST_SHA256: string | undefined;
declare const OPENPROSE_WINDOWS_HOST_ADMISSION: boolean | undefined;
declare const OPENPROSE_CODEX_INSTRUCTION_PLACEMENT: string | undefined;

export type CodexInstructionPlacement = "framed" | "developer" | "base";
// Explicit build selection for the port comparison; ambient runtime
// environment variables cannot change the installed binary's placement.
export const CODEX_INSTRUCTION_PLACEMENT: CodexInstructionPlacement =
  typeof OPENPROSE_CODEX_INSTRUCTION_PLACEMENT === "string"
    ? OPENPROSE_CODEX_INSTRUCTION_PLACEMENT as CodexInstructionPlacement
    : "framed";

export const RUNNER_BUILD_COMMIT =
  typeof OPENPROSE_BUILD_COMMIT === "string" && OPENPROSE_BUILD_COMMIT.length > 0
    ? OPENPROSE_BUILD_COMMIT
    : "development";

// The workspace package version, for source-level runs only. It is a literal
// rather than an import of package.json so the bundle never embeds the
// package manifest (its scripts name developer builds); test/build.test.ts
// keeps it equal to package.json.
export const SOURCE_PACKAGE_VERSION = "0.1.0";

// Standalone builds replace this identifier with a validated exact SemVer.
// Source-level tests deliberately fall back to the workspace package version.
export const RUNNER_VERSION =
  typeof OPENPROSE_BUILD_VERSION === "string" && OPENPROSE_BUILD_VERSION.length > 0
    ? OPENPROSE_BUILD_VERSION
    : SOURCE_PACKAGE_VERSION;

// Source-level tests deliberately retain hermetic seams. Every standalone
// build injects this constant explicitly, and release builds inject false.
// Call sites that read test-seam environment variables also guard inline on
// OPENPROSE_TEST_SEAMS (see cli.ts): an imported constant survives Bun's
// dead-code elimination, an inline define does not.
export const TEST_SEAMS_ENABLED =
  typeof OPENPROSE_TEST_SEAMS === "boolean" ? OPENPROSE_TEST_SEAMS : true;

// Standalone builds inject this independently from test-seam admission. An
// ordinary local build is development-profile even though its seams are off;
// only the explicit release build reports release.
export const BUILD_PROFILE =
  typeof OPENPROSE_BUILD_PROFILE === "string"
    ? OPENPROSE_BUILD_PROFILE
    : "development";

const compiledWindowsHostSha256 =
  typeof OPENPROSE_WINDOWS_HOST_SHA256 === "string" ? OPENPROSE_WINDOWS_HOST_SHA256 : "";

export const WINDOWS_HOST_EXPECTED_SHA256 = /^[0-9a-f]{64}$/.test(compiledWindowsHostSha256)
  ? compiledWindowsHostSha256
  : null;

// This is a compile-time promotion decision. Runtime environment variables and
// helper self-report can never turn the Windows path on.
export const WINDOWS_HOST_ADMISSION_ENABLED =
  typeof OPENPROSE_WINDOWS_HOST_ADMISSION === "boolean" ? OPENPROSE_WINDOWS_HOST_ADMISSION : false;


// PROSE_DEV_BUILD (the OpenProse developer endpoint build) is deliberately not
// exported from here: an imported constant survives Bun's dead-code
// elimination, so the one guard lives inline in service/endpoint.ts.

declare const OPENPROSE_KERNEL_STARTUP: boolean | undefined;
export const PUBLISHED_KERNEL_STARTUP = typeof OPENPROSE_KERNEL_STARTUP === "boolean" ? OPENPROSE_KERNEL_STARTUP : false;
