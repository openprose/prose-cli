#!/usr/bin/env node
"use strict";

const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const { createHash } = require("node:crypto");

const COHORT = __OPENPROSE_COHORT__;
const VERSION = COHORT.version;
const SAME_NAME = COHORT.schema === "openprose.npm-cohort/3";
const KERNEL_COHORT = SAME_NAME || COHORT.schema === "openprose.npm-cohort/2";
const platformVersion = (id) => SAME_NAME ? `${VERSION}-${id}` : VERSION;
const PACKAGE_PREFIX = "@openprose/prose-cli-";
const WINDOWS_HOST = "bin/openprose-windows-process-host.exe";
const MINIMUM_GLIBC = "2.34";
const LINUX_EXECUTION_EVIDENCE = "ubuntu-22.04-only";
const NODE_MINIMUM = [22, 22, 3];
const COHORT_KEYS = [
  "admittedPlatforms",
  "image",
  "publicationAuthorized",
  "purpose",
  "releaseChannel",
  "releaseEligible",
  "schema",
  "semanticStatus",
  "sourceRevision",
  "version",
];
const KERNEL_COHORT_KEYS = [
  "admittedPlatforms", "embeddedDiagnosticImage", "imageSource", "kernelPolicy",
  "publicationAuthorized", "purpose", "releaseChannel", "releaseEligible",
  "schema", "semanticStatus", "sourceRevision", "version",
];
const DIAGNOSTIC_IMAGE_KEYS = [
  "formatVersion", "manifestSha256", "purpose", "sha256", "version",
];
const KERNEL_POLICY_KEYS = ["entrypoint", "pinning", "resolution", "schema"];
const IMAGE_KEYS = [
  "formatVersion",
  "manifestSha256",
  "purpose",
  "releaseEligible",
  "sha256",
  "version",
];
const BUN_RUNTIME_BY_PLATFORM = {
  "darwin-arm64": { compileTarget: "bun-darwin-arm64", runtimeVariant: "native" },
  "darwin-x64": { compileTarget: "bun-darwin-x64-baseline", runtimeVariant: "baseline" },
  "linux-arm64-gnu": { compileTarget: "bun-linux-arm64", runtimeVariant: "native" },
  "linux-x64-gnu": { compileTarget: "bun-linux-x64-baseline", runtimeVariant: "baseline" },
  "win32-x64": { compileTarget: "bun-windows-x64-baseline", runtimeVariant: "baseline" },
};
const SUPPORTED = new Set(COHORT.admittedPlatforms);

function exactKeys(value, expected) {
  return value !== null
    && typeof value === "object"
    && !Array.isArray(value)
    && JSON.stringify(Object.keys(value).sort()) === JSON.stringify(expected);
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

function validateCohort(value, label) {
  if (
    !(KERNEL_COHORT
      ? exactKeys(value, KERNEL_COHORT_KEYS)
        && exactKeys(value.embeddedDiagnosticImage, DIAGNOSTIC_IMAGE_KEYS)
        && exactKeys(value.kernelPolicy, KERNEL_POLICY_KEYS)
      : exactKeys(value, COHORT_KEYS) && exactKeys(value.image, IMAGE_KEYS))
    || canonicalJson(value) !== canonicalJson(COHORT)
  ) {
    throw new Error(`${label} cohort is unknown, incomplete, or differs from this launcher`);
  }
}

function validateNodeRuntime() {
  const detected = dottedVersion(process.versions?.node);
  if (detected === undefined || compareVersions(detected, NODE_MINIMUM) < 0) {
    throw new Error(
      `requires Node.js >=${NODE_MINIMUM.join(".")}; detected ${process.versions?.node ?? "unknown"}`,
    );
  }
}

function linuxLibc() {
  const report = typeof process.report?.getReport === "function" ? process.report.getReport() : null;
  return report?.header?.glibcVersionRuntime ? "gnu" : "musl";
}

function platformId() {
  const cpu = process.arch === "x64" || process.arch === "arm64" ? process.arch : process.arch;
  if (process.platform === "linux") return `linux-${cpu}-${linuxLibc()}`;
  return `${process.platform}-${cpu}`;
}

function dottedVersion(value) {
  if (typeof value !== "string" || !/^[0-9]+\.[0-9]+(?:\.[0-9]+)?$/.test(value)) return undefined;
  const parts = value.split(".").map(Number);
  if (parts.some((part) => !Number.isSafeInteger(part) || part > 1000000)) return undefined;
  return parts;
}

function compareVersions(left, right) {
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    const difference = (left[index] ?? 0) - (right[index] ?? 0);
    if (difference !== 0) return difference;
  }
  return 0;
}

function validateLinuxRuntime(manifest, id) {
  if (!id.startsWith("linux-")) return;
  if (
    manifest.openproseMinimumGlibc !== MINIMUM_GLIBC
    || manifest.openproseLinuxExecutionEvidence !== LINUX_EXECUTION_EVIDENCE
  ) {
    throw new Error("package Linux runtime floor is missing or invalid");
  }
  const required = dottedVersion(manifest.openproseRequiredGlibcMaximum);
  const floor = dottedVersion(MINIMUM_GLIBC);
  const runtime = dottedVersion(process.report?.getReport?.()?.header?.glibcVersionRuntime);
  if (required === undefined || floor === undefined || compareVersions(required, floor) > 0) {
    throw new Error("package ELF glibc requirement is missing or exceeds its declared floor");
  }
  if (runtime === undefined) {
    throw new Error("cannot determine the current glibc version");
  }
  if (compareVersions(runtime, floor) < 0) {
    throw new Error(
      `requires glibc >= ${MINIMUM_GLIBC}; detected ${runtime.join(".")}. `
      + "Linux execution evidence is limited to Ubuntu 22.04; use that environment or a future compatible build",
    );
  }
}

function fail(message) {
  process.stderr.write(`OpenProse CLI: ${message}\n`);
  process.exitCode = 1;
}

function sha256File(file) {
  return createHash("sha256").update(fs.readFileSync(file)).digest("hex");
}

function samePath(left, right) {
  const normalizedLeft = path.normalize(left);
  const normalizedRight = path.normalize(right);
  return process.platform === "win32"
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}

function directClosure(installRoot, leaf, finalType, label) {
  const root = path.resolve(installRoot);
  const target = path.resolve(leaf);
  const relative = path.relative(root, target);
  if (relative === "" || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error(`${label} is outside the expected install root`);
  }
  const paths = [root];
  let current = root;
  for (const component of relative.split(path.sep)) {
    current = path.join(current, component);
    paths.push(current);
  }
  for (let index = 0; index < paths.length; index += 1) {
    const candidate = paths[index];
    const info = fs.lstatSync(candidate);
    const expectedType = index === paths.length - 1 ? finalType : "directory";
    if (info.isSymbolicLink()) {
      throw new Error(`${label} ancestry must not contain symlinks`);
    }
    if (
      (expectedType === "directory" && !info.isDirectory())
      || (expectedType === "file" && !info.isFile())
    ) {
      throw new Error(`${label} ancestry has an unexpected file type`);
    }
    if (!samePath(fs.realpathSync(candidate), path.resolve(candidate))) {
      throw new Error(`${label} ancestry contains a parent alias`);
    }
  }
  return target;
}

function platformManifestPath(packageName) {
  const metaRoot = path.resolve(__dirname, "..");
  const scopeRoot = path.dirname(metaRoot);
  const installRoot = path.dirname(scopeRoot);
  if (
    path.basename(metaRoot) !== "prose-cli"
    || path.basename(scopeRoot) !== "@openprose"
    || path.basename(installRoot) !== "node_modules"
  ) {
    throw new Error("launcher is not inside the expected @openprose/prose-cli package");
  }
  directClosure(installRoot, __filename, "file", "meta package launcher");
  directClosure(installRoot, path.join(metaRoot, "package.json"), "file", "meta package");
  const platformBasename = packageName.slice("@openprose/".length);
  const candidates = [
    path.join(metaRoot, "node_modules", "@openprose", platformBasename, "package.json"),
    path.join(scopeRoot, platformBasename, "package.json"),
  ];
  for (const candidate of candidates) {
    try {
      const manifestPath = directClosure(
        installRoot,
        candidate,
        "file",
        "platform package manifest",
      );
      const platformRoot = path.dirname(manifestPath);
      if (path.basename(platformRoot) !== platformBasename) {
        throw new Error("platform package root name is not exact");
      }
      return {
        installRoot,
        metaRoot,
        metaManifestPath: path.join(metaRoot, "package.json"),
        manifestPath,
        platformRoot,
      };
    } catch (error) {
      if (error?.code !== "ENOENT" && error?.code !== "ENOTDIR") throw error;
    }
  }
  return undefined;
}

function currentInstallPrefix() {
  const metaRoot = path.resolve(__dirname, "..");
  const scopeRoot = path.dirname(metaRoot);
  const installRoot = path.dirname(scopeRoot);
  if (path.basename(installRoot) !== "node_modules") {
    throw new Error("cannot derive the current npm installation prefix");
  }
  const parent = path.dirname(installRoot);
  return path.basename(parent) === "lib" ? path.dirname(parent) : parent;
}

function posixShellQuote(value) {
  return `'${String(value).replaceAll("'", "'\\''")}'`;
}

const NON_COPYABLE_REPAIR_GUIDANCE = "No copyable registry repair command is available because "
  + "the npm installation prefix contains terminal control characters; reinstall OpenProse in a "
  + "path without control characters.";

function hasTerminalControl(value) {
  return [...String(value)].some((character) => {
    const codePoint = character.codePointAt(0);
    return (codePoint >= 0 && codePoint <= 0x1f)
      || (codePoint >= 0x7f && codePoint <= 0x9f)
      || codePoint === 0x2028
      || codePoint === 0x2029;
  });
}

function terminalSafeScalar(value) {
  let rendered = "";
  for (const character of String(value)) {
    const codePoint = character.codePointAt(0);
    if (character === "\\") rendered += "\\\\";
    else if (character === "\b") rendered += "\\b";
    else if (character === "\t") rendered += "\\t";
    else if (character === "\n") rendered += "\\n";
    else if (character === "\f") rendered += "\\f";
    else if (character === "\r") rendered += "\\r";
    else if (
      (codePoint >= 0 && codePoint <= 0x1f)
      || (codePoint >= 0x7f && codePoint <= 0x9f)
      || codePoint === 0x2028
      || codePoint === 0x2029
    ) {
      rendered += `\\u{${codePoint.toString(16).toUpperCase().padStart(4, "0")}}`;
    } else rendered += character;
  }
  return rendered;
}

function terminalSafeError(error) {
  return terminalSafeScalar(error instanceof Error ? error.message : String(error));
}

function registryRepairCommand(packageName) {
  const prefix = currentInstallPrefix();
  if (hasTerminalControl(prefix)) return NON_COPYABLE_REPAIR_GUIDANCE;
  const repairArguments = [
    "install",
    "--global",
    "--ignore-scripts",
    "--prefix",
    prefix,
    SAME_NAME ? `${packageName}@npm:@openprose/prose-cli@${platformVersion(platformId())}` : `${packageName}@${VERSION}`,
    `@openprose/prose-cli@${VERSION}`,
  ];
  if (process.platform === "win32") {
    return `npm argument vector (quote for your Windows shell): ${JSON.stringify(repairArguments)}`;
  }
  return `npm install --global --ignore-scripts --prefix ${posixShellQuote(prefix)} `
    + `${posixShellQuote(SAME_NAME ? `${packageName}@npm:@openprose/prose-cli@${platformVersion(platformId())}` : `${packageName}@${VERSION}`)} `
    + posixShellQuote(`@openprose/prose-cli@${VERSION}`);
}

function validateMetaManifest(location, manifest, manifestSha256, launcherBytes) {
  if (manifest.name !== "@openprose/prose-cli" || manifest.version !== VERSION) {
    throw new Error("meta package identity differs from this launcher");
  }
  validateCohort(manifest.openproseCohort, "meta package");
  if (
    !exactKeys(manifest.openproseLauncher, ["byteLength", "path", "sha256"])
    || manifest.openproseLauncher.path !== "bin/prose.js"
    || manifest.openproseLauncher.byteLength !== launcherBytes.length
    || !/^[0-9a-f]{64}$/.test(manifest.openproseLauncher.sha256 ?? "")
    || createHash("sha256").update(launcherBytes).digest("hex") !== manifest.openproseLauncher.sha256
  ) {
    throw new Error("meta package launcher identity differs");
  }
  const expectedOptional = Object.fromEntries(
    COHORT.admittedPlatforms.map((platform) => [`${PACKAGE_PREFIX}${platform}`, SAME_NAME ? `npm:@openprose/prose-cli@${platformVersion(platform)}` : VERSION]),
  );
  if (canonicalJson(manifest.optionalDependencies) !== canonicalJson(expectedOptional)) {
    throw new Error("meta package optional platform cohort differs");
  }
  return {
    metaManifestSha256: manifestSha256,
    launcherByteLength: launcherBytes.length,
    launcherSha256: manifest.openproseLauncher.sha256,
  };
}

function resolveBinary(location, manifest, manifestSha256, packageName, id, metaIdentity) {
  const {
    installRoot, metaRoot, metaManifestPath, manifestPath, platformRoot: packageRoot,
  } = location;
  if (manifest.name !== (SAME_NAME ? "@openprose/prose-cli" : packageName)) {
    throw new Error(`package identity is ${manifest.name ?? "missing"}, expected ${packageName}`);
  }
  validateCohort(manifest.openproseCohort, "platform package");
  if (
    manifest.openprosePlatform !== id
    || manifest.openproseSourceRevision !== COHORT.sourceRevision
    || (KERNEL_COHORT
      ? manifest.openproseImage !== undefined
        || canonicalJson(manifest.openproseEmbeddedDiagnosticImage) !== canonicalJson(COHORT.embeddedDiagnosticImage)
        || canonicalJson(manifest.openproseKernelPolicy) !== canonicalJson(COHORT.kernelPolicy)
        || manifest.openproseImageSource !== COHORT.imageSource
      : canonicalJson(manifest.openproseImage) !== canonicalJson(COHORT.image))
    || manifest.openproseBunCompileTarget !== BUN_RUNTIME_BY_PLATFORM[id]?.compileTarget
    || manifest.openproseBunRuntimeVariant !== BUN_RUNTIME_BY_PLATFORM[id]?.runtimeVariant
  ) {
    throw new Error("platform package cohort identity differs");
  }
  const expectedRelative = id.startsWith("win32-") ? "bin/prose.exe" : "bin/prose";
  if (manifest.openproseBinary !== expectedRelative) {
    throw new Error(`executable path is invalid; expected ${expectedRelative}`);
  }
  if (!Number.isSafeInteger(manifest.openproseBinaryByteLength) || manifest.openproseBinaryByteLength < 1) {
    throw new Error("executable byte length is missing or invalid");
  }
  if (!/^[0-9a-f]{64}$/.test(manifest.openproseBinarySha256 ?? "")) {
    throw new Error("executable SHA-256 is missing or invalid");
  }
  directClosure(installRoot, packageRoot, "directory", "platform package root");
  directClosure(packageRoot, manifestPath, "file", "platform package manifest");
  const declaredBinary = path.join(packageRoot, manifest.openproseBinary);
  directClosure(packageRoot, declaredBinary, "file", "platform executable");
  const declaredStat = fs.lstatSync(declaredBinary);
  if (declaredStat.size !== manifest.openproseBinaryByteLength) {
    throw new Error("executable integrity mismatch (byte length)");
  }
  if (sha256File(declaredBinary) !== manifest.openproseBinarySha256) {
    throw new Error("executable integrity mismatch (SHA-256)");
  }
  if (id.startsWith("win32-")) {
    if (manifest.openproseWindowsProcessHost !== WINDOWS_HOST) {
      throw new Error(`Windows process host path is invalid; expected ${WINDOWS_HOST}`);
    }
    if (
      !Number.isSafeInteger(manifest.openproseWindowsProcessHostByteLength)
      || manifest.openproseWindowsProcessHostByteLength < 1
    ) {
      throw new Error("Windows process host byte length is missing or invalid");
    }
    if (!/^[0-9a-f]{64}$/.test(manifest.openproseWindowsProcessHostSha256 ?? "")) {
      throw new Error("Windows process host SHA-256 is missing or invalid");
    }
    if (manifest.openproseWindowsProcessHostAdmission !== false) {
      throw new Error("Windows process host admission must remain false in this package");
    }
    const declaredHost = path.join(packageRoot, manifest.openproseWindowsProcessHost);
    directClosure(packageRoot, declaredHost, "file", "Windows process host");
    const hostStat = fs.lstatSync(declaredHost);
    if (hostStat.size !== manifest.openproseWindowsProcessHostByteLength) {
      throw new Error("Windows process host integrity mismatch (byte length)");
    }
    if (sha256File(declaredHost) !== manifest.openproseWindowsProcessHostSha256) {
      throw new Error("Windows process host integrity mismatch (SHA-256)");
    }
    return {
      binary: declaredBinary,
      host: declaredHost,
      installRoot,
      metaRoot,
      metaManifestPath,
      manifestPath,
      platformRoot: packageRoot,
      binaryByteLength: manifest.openproseBinaryByteLength,
      binarySha256: manifest.openproseBinarySha256,
      hostByteLength: manifest.openproseWindowsProcessHostByteLength,
      hostSha256: manifest.openproseWindowsProcessHostSha256,
      manifestSha256,
      ...metaIdentity,
    };
  }
  return {
    binary: declaredBinary,
    host: undefined,
    installRoot,
    metaRoot,
    metaManifestPath,
    manifestPath,
    platformRoot: packageRoot,
    binaryByteLength: manifest.openproseBinaryByteLength,
    binarySha256: manifest.openproseBinarySha256,
    manifestSha256,
    ...metaIdentity,
  };
}

function reauthenticateResolvedBinary(resolved) {
  directClosure(resolved.installRoot, __filename, "file", "meta package launcher");
  directClosure(
    resolved.installRoot,
    resolved.metaManifestPath,
    "file",
    "meta package",
  );
  directClosure(
    resolved.installRoot,
    resolved.platformRoot,
    "directory",
    "platform package root",
  );
  directClosure(
    resolved.platformRoot,
    resolved.manifestPath,
    "file",
    "platform package manifest",
  );
  directClosure(resolved.platformRoot, resolved.binary, "file", "platform executable");
  const binaryStat = fs.lstatSync(resolved.binary);
  const launcherStat = fs.lstatSync(__filename);
  if (
    sha256File(resolved.metaManifestPath) !== resolved.metaManifestSha256
    || launcherStat.size !== resolved.launcherByteLength
    || sha256File(__filename) !== resolved.launcherSha256
    || sha256File(resolved.manifestPath) !== resolved.manifestSha256
    || binaryStat.size !== resolved.binaryByteLength
    || sha256File(resolved.binary) !== resolved.binarySha256
  ) {
    throw new Error("package closure changed before spawn");
  }
  if (resolved.host !== undefined) {
    directClosure(resolved.platformRoot, resolved.host, "file", "Windows process host");
    const hostStat = fs.lstatSync(resolved.host);
    if (
      hostStat.size !== resolved.hostByteLength
      || sha256File(resolved.host) !== resolved.hostSha256
    ) {
      throw new Error("Windows process host changed before spawn");
    }
  }
}

let runtimeValid = true;
try {
  validateNodeRuntime();
} catch (error) {
  runtimeValid = false;
  fail(terminalSafeError(error));
}

const id = platformId();
if (runtimeValid && !SUPPORTED.has(id)) {
  fail(
    `platform ${id} is unsupported by @openprose/prose-cli@${VERSION}. `
      + `Supported platforms: ${[...SUPPORTED].join(", ")}.`,
  );
} else if (runtimeValid) {
  const packageName = `${PACKAGE_PREFIX}${id}`;
  let location;
  try {
    location = platformManifestPath(packageName);
  } catch (error) {
    fail(`cannot resolve ${packageName} from this installation: ${terminalSafeError(error)}`);
  }
  if (location === undefined && process.exitCode === undefined) {
    fail(
      `required optional platform package ${packageName}@${VERSION} is not installed. `
        + `Registry repair in this installation prefix: ${registryRepairCommand(packageName)}`,
    );
  }

  if (location !== undefined) {
    let manifest;
    let manifestSha256;
    let metaIdentity;
    try {
      const metaManifestBytes = fs.readFileSync(location.metaManifestPath);
      const metaManifest = JSON.parse(metaManifestBytes.toString("utf8"));
      const launcherBytes = fs.readFileSync(__filename);
      metaIdentity = validateMetaManifest(
        location,
        metaManifest,
        createHash("sha256").update(metaManifestBytes).digest("hex"),
        launcherBytes,
      );
      const manifestBytes = fs.readFileSync(location.manifestPath);
      manifest = JSON.parse(manifestBytes.toString("utf8"));
      manifestSha256 = createHash("sha256").update(manifestBytes).digest("hex");
    } catch (error) {
      fail(`cannot read ${packageName}: ${terminalSafeError(error)}`);
    }
    if (manifest !== undefined && metaIdentity !== undefined && manifest.version !== platformVersion(id)) {
      const installedVersion = terminalSafeScalar(manifest.version ?? "unknown");
      fail(
        `installed ${packageName}@${installedVersion}, but this launcher requires exactly ${platformVersion(id)}. `
          + `Registry repair in this installation prefix: ${registryRepairCommand(packageName)}`,
      );
    } else if (manifest !== undefined && metaIdentity !== undefined) {
      try {
        validateLinuxRuntime(manifest, id);
      } catch (error) {
        fail(
          `installed ${packageName}@${VERSION} cannot run on this Linux runtime: `
          + `${terminalSafeError(error)}.`,
        );
      }
      let resolved;
      if (process.exitCode === undefined) {
        try {
          resolved = resolveBinary(
            location, manifest, manifestSha256, packageName, id, metaIdentity,
          );
        } catch (error) {
          fail(
            `installed ${packageName}@${VERSION} executable is invalid: `
            + `${terminalSafeError(error)}. `
            + `Registry repair in this installation prefix: ${registryRepairCommand(packageName)}`,
          );
        }
      }
      if (resolved !== undefined) {
        try {
          reauthenticateResolvedBinary(resolved);
        } catch (error) {
          fail(
            `installed ${packageName}@${VERSION} changed before execution: `
            + `${terminalSafeError(error)}. `
            + `Registry repair in this installation prefix: ${registryRepairCommand(packageName)}`,
          );
        }
      }
      if (resolved !== undefined && process.exitCode === undefined) {
        const child = spawn(resolved.binary, process.argv.slice(2), {
          stdio: "inherit",
          windowsHide: false,
          shell: false,
        });
        const signals = process.platform === "win32" ? ["SIGINT", "SIGTERM"] : ["SIGINT", "SIGTERM", "SIGHUP"];
        const handlers = new Map();
        for (const name of signals) {
          const handler = () => {
            if (child.exitCode === null && child.signalCode === null) child.kill(name);
          };
          handlers.set(name, handler);
          process.on(name, handler);
        }
        const removeHandlers = () => {
          for (const [name, handler] of handlers) process.off(name, handler);
        };
        child.once("error", (error) => {
          removeHandlers();
          fail(`could not start ${packageName}@${VERSION}: ${terminalSafeError(error)}`);
        });
        child.once("exit", (code, childSignal) => {
          removeHandlers();
          if (childSignal !== null && process.platform !== "win32") {
            process.kill(process.pid, childSignal);
          } else {
            process.exitCode = code ?? 1;
          }
        });
      }
    }
  }
}
