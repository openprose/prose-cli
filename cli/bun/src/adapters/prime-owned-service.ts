import { randomUUID } from "node:crypto";
import { chmod, link, lstat, open, opendir, readFile, realpath, rmdir, unlink } from "node:fs/promises";
import { createConnection, type Socket } from "node:net";
import { basename, dirname, join, resolve } from "node:path";
import { failure } from "../core/errors";
import { RunnerFailure } from "../core/types";

const RECOVERY_DIRECTORY_PREFIX = "openprose-prime-";
const RECOVERY_MARKER = ".openprose-prime-cleanup.json";
const MAX_RECOVERY_MARKER_BYTES = 2_048;
const MAX_RECOVERY_ENTRIES = 4_096;
const MAX_RECOVERY_SCRUB_BYTES = 64 * 1024 * 1024;
const MAX_RECOVERY_DEPTH = 16;

const DEFAULT_POLICY: PrimeOwnedServicePolicy = {
  ioTimeoutMs: 5_000,
  shutdownDeadlineMs: 5_000,
  pollIntervalMs: 25,
  maximumFrameBytes: 65_536,
};

export interface PrimeOwnedServicePolicy {
  ioTimeoutMs: number;
  shutdownDeadlineMs: number;
  pollIntervalMs: number;
  maximumFrameBytes: number;
}

export interface PrimeOwnedServiceSettlement {
  directory: string;
  socketPath: string;
  detectedVersion: string | null;
  policy?: PrimeOwnedServicePolicy;
}

export interface PrimeRecovery {
  handle: string;
}

interface RecoveryMarker {
  schema: "openprose.prime-cleanup-marker/1";
  handle: string;
  directory: string;
  socket: "prime.sock";
  detectedVersion: "0.7.0" | "0.8.1";
}

export async function preparePrimeRecovery(input: {
  temporaryRoot: string;
  directory: string;
  socketPath: string;
  detectedVersion: string | null;
}): Promise<PrimeRecovery | null> {
  if (process.platform === "win32") return null;
  if (input.detectedVersion !== "0.7.0" && input.detectedVersion !== "0.8.1") {
    throw cleanupFailure("recovery-version-validation");
  }
  const directory = await validateRecoveryDirectory(input.temporaryRoot, input.directory);
  const socketPath = join(directory, "prime.sock");
  const socketParent = await realpath(dirname(resolve(input.socketPath))).catch(() => null);
  if (socketParent !== directory || basename(input.socketPath) !== "prime.sock") {
    throw cleanupFailure("recovery-socket-placement");
  }
  await scrubDirectoryExceptSocket(directory, socketPath);
  const socketStat = await lstat(socketPath).catch((caught: NodeJS.ErrnoException) => {
    if (caught.code === "ENOENT") return null;
    throw cleanupFailure("recovery-socket-inspection");
  });
  if (socketStat === null) {
    await rmdir(directory).catch(() => { throw cleanupFailure("recovery-empty-directory-removal"); });
    return null;
  }
  if (!socketStat.isSocket()) throw cleanupFailure("recovery-socket-validation");
  const directoryName = basename(directory);
  if (!validDirectoryName(directoryName)) throw cleanupFailure("recovery-directory-name");
  const handle = `prime-v1.${directoryName}.${randomUUID()}`;
  const marker: RecoveryMarker = {
    schema: "openprose.prime-cleanup-marker/1",
    handle,
    directory: directoryName,
    socket: "prime.sock",
    detectedVersion: input.detectedVersion,
  };
  await writeRecoveryMarker(join(directory, RECOVERY_MARKER), marker);
  return { handle };
}

export async function recoverPrimeOwnedService(input: {
  temporaryRoot: string;
  handle: string;
  policy?: PrimeOwnedServicePolicy;
  /** Test seam for a failure between marker retirement and final rmdir. */
  testBeforeFinalDirectoryRemoval?: (directory: string) => void | Promise<void>;
}): Promise<void> {
  if (process.platform === "win32") {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handles are supported only on POSIX hosts" });
  }
  const directoryName = parseRecoveryHandle(input.handle);
  const root = await realpath(input.temporaryRoot).catch(() => {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup temporary root is unavailable" });
  });
  const ticketPath = recoveryTicketPath(root, input.handle);
  const lexicalDirectory = join(input.temporaryRoot, directoryName);
  const directoryStat = await lstat(lexicalDirectory).catch((caught: NodeJS.ErrnoException) => {
    if (caught.code === "ENOENT") return null;
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
  });
  if (directoryStat === null) {
    const marker = await readRecoveryMarker(ticketPath);
    authenticateRecoveryMarker(marker, input.handle, directoryName);
    await unlink(ticketPath).catch(() => {
      throw primeRecoveryFailure(cleanupFailure("recovery-ticket-removal"), { handle: input.handle });
    });
    return;
  }

  const directory = await validateRecoveryDirectory(root, lexicalDirectory).catch((caught) => {
    throw primeRecoveryFailure(caught, { handle: input.handle });
  });
  const markerPath = join(directory, RECOVERY_MARKER);
  const markerStat = await lstat(markerPath).catch((caught: NodeJS.ErrnoException) => {
    if (caught.code === "ENOENT") return null;
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
  });
  if (markerStat === null) {
    const marker = await readRecoveryMarker(ticketPath);
    authenticateRecoveryMarker(marker, input.handle, directoryName);
    await scrubFinalizingDirectory(directory).catch((caught) => {
      throw primeRecoveryFailure(caught, { handle: input.handle });
    });
    await validateFinalizingDirectory(directory).catch((caught) => {
      throw primeRecoveryFailure(caught, { handle: input.handle });
    });
    await rmdir(directory).catch(() => {
      throw primeRecoveryFailure(cleanupFailure("recovery-directory-removal"), { handle: input.handle });
    });
    await unlink(ticketPath).catch(() => {
      throw primeRecoveryFailure(cleanupFailure("recovery-ticket-removal"), { handle: input.handle });
    });
    return;
  }
  const marker = await readRecoveryMarker(markerPath);
  authenticateRecoveryMarker(marker, input.handle, directoryName);
  await validateRecoveryContents(directory);
  const socketPath = join(lexicalDirectory, marker.socket);
  try {
    await settlePrimeOwnedService({
      directory: lexicalDirectory,
      socketPath,
      detectedVersion: marker.detectedVersion,
      ...(input.policy === undefined ? {} : { policy: input.policy }),
    });
  } catch (caught) {
    throw primeRecoveryFailure(caught, { handle: input.handle });
  }
  await validateRecoveryDirectory(root, directory);
  const markerAfter = await readRecoveryMarker(markerPath);
  if (JSON.stringify(markerAfter) !== JSON.stringify(marker)) {
    throw primeRecoveryFailure(cleanupFailure("recovery-marker-changed"), {
      handle: input.handle,
    });
  }
  await validateRecoveryContents(directory);
  const canonicalSocketPath = join(directory, marker.socket);
  const socketStat = await lstat(canonicalSocketPath).catch((caught: NodeJS.ErrnoException) => {
    if (caught.code === "ENOENT") return null;
    throw primeRecoveryFailure(cleanupFailure("recovery-stale-socket-inspection"), {
      handle: input.handle,
    });
  });
  if (socketStat !== null) {
    if (!socketStat.isSocket()) throw primeRecoveryFailure(cleanupFailure("recovery-stale-socket-validation"), {
      handle: input.handle,
    });
    await unlink(canonicalSocketPath).catch(() => { throw primeRecoveryFailure(cleanupFailure("recovery-stale-socket-removal"), {
      handle: input.handle,
    }); });
  }
  const ticketStat = await lstat(ticketPath).catch((caught: NodeJS.ErrnoException) => {
    if (caught.code === "ENOENT") return null;
    throw primeRecoveryFailure(cleanupFailure("recovery-ticket-inspection"), { handle: input.handle });
  });
  if (ticketStat === null) {
    await link(markerPath, ticketPath).catch(() => {
      throw primeRecoveryFailure(cleanupFailure("recovery-ticket-create"), { handle: input.handle });
    });
    try {
      const ticket = await readRecoveryMarker(ticketPath);
      authenticateRecoveryMarker(ticket, input.handle, directoryName);
      if (JSON.stringify(ticket) !== JSON.stringify(marker)) throw cleanupFailure("recovery-ticket-validation");
    } catch {
      await unlink(ticketPath).catch(() => undefined);
      throw primeRecoveryFailure(cleanupFailure("recovery-ticket-validation"), { handle: input.handle });
    }
  } else {
    try {
      const ticket = await readRecoveryMarker(ticketPath);
      authenticateRecoveryMarker(ticket, input.handle, directoryName);
      if (JSON.stringify(ticket) !== JSON.stringify(marker)) throw cleanupFailure("recovery-ticket-collision");
    } catch {
      throw primeRecoveryFailure(cleanupFailure("recovery-ticket-collision"), { handle: input.handle });
    }
  }
  try {
    await unlink(markerPath);
  } catch {
    await unlink(ticketPath).catch(() => undefined);
    throw primeRecoveryFailure(cleanupFailure("recovery-marker-removal"), { handle: input.handle });
  }
  try {
    await input.testBeforeFinalDirectoryRemoval?.(directory);
    await rmdir(directory);
  } catch {
    throw primeRecoveryFailure(cleanupFailure("recovery-directory-removal"), { handle: input.handle });
  }
  await unlink(ticketPath).catch(() => {
    throw primeRecoveryFailure(cleanupFailure("recovery-ticket-removal"), { handle: input.handle });
  });
}

export function primeRecoveryFailure(caught: unknown, recovery: PrimeRecovery): RunnerFailure {
  const details = caught instanceof RunnerFailure ? caught.details : undefined;
  return failure("PROCESS_CLEANUP_FAILED", {
    ...details,
    adapterId: "prime/rpc",
    fallbackAttempted: false,
    cleanupHandle: recovery.handle,
    cleanupArgv: ["cli", "cleanup", "prime", recovery.handle],
    sensitiveFilesRemoved: true,
  });
}

/** Settle only the Prime service allocated for this exact wrapper invocation. */
export async function settlePrimeOwnedService(input: PrimeOwnedServiceSettlement): Promise<void> {
  const policy = input.policy ?? DEFAULT_POLICY;
  if (process.platform === "win32") throw cleanupFailure("unsupported-platform");
  const directory = resolve(input.directory);
  const socketPath = resolve(input.socketPath);
  if (dirname(socketPath) !== directory || socketPath !== input.socketPath || directory !== input.directory) {
    throw cleanupFailure("owned-path-validation");
  }
  const directoryStat = await lstat(directory).catch(() => null);
  if (directoryStat === null || !directoryStat.isDirectory() || (directoryStat.mode & 0o777) !== 0o700) {
    throw cleanupFailure("owned-directory-validation");
  }
  const socketStat = await lstat(socketPath).catch((caught: NodeJS.ErrnoException) => {
    if (caught.code === "ENOENT") return null;
    throw cleanupFailure("owned-socket-inspection");
  });
  if (socketStat === null) return;
  if (!socketStat.isSocket()) throw cleanupFailure("owned-socket-validation");

  const socket = await connect(socketPath, policy.ioTimeoutMs);
  if (socket === null) return;
  const buffered = { value: Buffer.alloc(0) };
  try {
    const hello = await readJsonLine(socket, buffered, policy, "daemon-hello");
    if (!isExpectedHello(hello, socketPath, input.detectedVersion)) {
      throw cleanupFailure("daemon-hello-validation");
    }
    const id = `openprose-shutdown-${randomUUID()}`;
    const envelope = {
      type: "command",
      id,
      protocol: { name: "prime-agent.daemon", version: 7 },
      clientId: "openprose-wrapper",
      command: { id, type: "shutdown", force: true },
    };
    await writeBounded(socket, `${JSON.stringify(envelope)}\n`, policy.ioTimeoutMs);
    const response = await readJsonLine(socket, buffered, policy, "shutdown-response");
    if (!isSuccessfulShutdown(response, id)) throw cleanupFailure("shutdown-response-validation");
  } finally {
    socket.destroy();
  }

  const deadline = performance.now() + policy.shutdownDeadlineMs;
  while (performance.now() < deadline) {
    const probe = await probeListening(socketPath, policy.ioTimeoutMs);
    if (probe === "gone") return;
    await Bun.sleep(policy.pollIntervalMs);
  }
  throw cleanupFailure("shutdown-listener-timeout");
}

function cleanupFailure(_phase: string) {
  return failure("PROCESS_CLEANUP_FAILED", {
    phase: "owned-service-settlement",
    processResource: "owned-prime-harness-service",
    adapterId: "prime/rpc",
    fallbackAttempted: false,
  });
}

async function validateRecoveryDirectory(temporaryRoot: string, directory: string): Promise<string> {
  const candidate = resolve(directory);
  const directoryStat = await lstat(candidate).catch(() => null);
  const uid = process.getuid?.();
  if (directoryStat === null
    || !directoryStat.isDirectory()
    || directoryStat.isSymbolicLink()
    || (directoryStat.mode & 0o777) !== 0o700
    || uid === undefined
    || directoryStat.uid !== uid) {
    throw cleanupFailure("recovery-directory-validation");
  }
  const root = await realpath(temporaryRoot).catch(() => { throw cleanupFailure("recovery-root-inspection"); });
  const canonical = await realpath(candidate).catch(() => { throw cleanupFailure("recovery-directory-inspection"); });
  if (dirname(canonical) !== root || !validDirectoryName(basename(canonical))) {
    throw cleanupFailure("recovery-directory-ancestry");
  }
  return canonical;
}

function validDirectoryName(name: string): boolean {
  if (!name.startsWith(RECOVERY_DIRECTORY_PREFIX)) return false;
  const suffix = name.slice(RECOVERY_DIRECTORY_PREFIX.length);
  return suffix.length >= 6 && suffix.length <= 64 && /^[A-Za-z0-9-]+$/.test(suffix);
}

function parseRecoveryHandle(handle: string): string {
  if (handle.length > 160 || !/^[\x20-\x7e]+$/.test(handle)) {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is invalid" });
  }
  const parts = handle.split(".");
  if (parts.length !== 3
    || parts[0] !== "prime-v1"
    || !validDirectoryName(parts[1] ?? "")
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(parts[2] ?? "")) {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is invalid" });
  }
  return parts[1]!;
}

function recoveryTicketPath(root: string, handle: string): string {
  const token = handle.split(".")[2];
  if (token === undefined) throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is invalid" });
  return join(root, `.openprose-prime-cleanup-${token}.json`);
}

function authenticateRecoveryMarker(marker: RecoveryMarker, handle: string, directoryName: string): void {
  if (marker.handle !== handle || marker.directory !== directoryName) {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
  }
}

async function validateFinalizingDirectory(directory: string): Promise<void> {
  const opened = await opendir(directory).catch(() => { throw cleanupFailure("recovery-directory-read"); });
  try {
    if (await opened.read() !== null) throw cleanupFailure("recovery-unexpected-finalizing-entry");
  } finally {
    try {
      await opened.close();
    } catch {
      // The stream may already have been closed by the runtime at EOF.
    }
  }
}

async function scrubDirectoryExceptSocket(directory: string, socketPath: string): Promise<void> {
  const budget = { entries: 0, bytes: 0 };
  await scrubDirectoryEntries(directory, socketPath, 0, budget);
}

async function scrubFinalizingDirectory(directory: string): Promise<void> {
  const budget = { entries: 0, bytes: 0 };
  await scrubDirectoryEntries(directory, null, 0, budget);
}

async function scrubDirectoryEntries(
  directory: string,
  preservedSocketPath: string | null,
  depth: number,
  budget: { entries: number; bytes: number },
): Promise<void> {
  const opened = await opendir(directory).catch(() => { throw cleanupFailure("recovery-scrub-read"); });
  try {
    for (;;) {
      const entry = await opened.read().catch(() => { throw cleanupFailure("recovery-scrub-read"); });
      if (entry === null) break;
      budget.entries += 1;
      if (budget.entries > MAX_RECOVERY_ENTRIES || depth > MAX_RECOVERY_DEPTH) {
        throw cleanupFailure("recovery-scrub-entry-limit");
      }
      const path = join(directory, entry.name);
      if (path === preservedSocketPath) {
        const stat = await lstat(path).catch(() => null);
        if (stat?.isSocket()) continue;
      }
      await removeTreeNoFollow(path, depth, budget);
    }
  } finally {
    try {
      await opened.close();
    } catch {
      // The stream may already have been closed by the runtime at EOF.
    }
  }
}

async function removeTreeNoFollow(
  path: string,
  depth: number,
  budget: { entries: number; bytes: number },
): Promise<void> {
  const stat = await lstat(path).catch(() => { throw cleanupFailure("recovery-scrub-inspection"); });
  if (stat.isDirectory() && !stat.isSymbolicLink()) {
    await scrubDirectoryEntries(path, null, depth + 1, budget);
    await rmdir(path).catch(() => { throw cleanupFailure("recovery-scrub-directory"); });
    return;
  }
  budget.bytes += stat.size;
  if (budget.bytes > MAX_RECOVERY_SCRUB_BYTES) throw cleanupFailure("recovery-scrub-byte-limit");
  await unlink(path).catch(() => { throw cleanupFailure("recovery-scrub-file"); });
}

async function writeRecoveryMarker(path: string, marker: RecoveryMarker): Promise<void> {
  const file = await open(path, "wx", 0o600).catch(() => { throw cleanupFailure("recovery-marker-create"); });
  try {
    await file.writeFile(JSON.stringify(marker));
    await file.sync();
  } catch {
    throw cleanupFailure("recovery-marker-write");
  } finally {
    await file.close();
  }
  await chmod(path, 0o600);
}

async function readRecoveryMarker(path: string): Promise<RecoveryMarker> {
  const stat = await lstat(path).catch(() => null);
  const uid = process.getuid?.();
  if (stat === null || !stat.isFile() || stat.isSymbolicLink() || (stat.mode & 0o777) !== 0o600
    || uid === undefined || stat.uid !== uid || stat.size > MAX_RECOVERY_MARKER_BYTES) {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
  }
  let value: unknown;
  try {
    value = JSON.parse(await readFile(path, "utf8"));
  } catch {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
  }
  if (!isRecord(value)
    || Object.keys(value).sort().join(",") !== "detectedVersion,directory,handle,schema,socket"
    || value.schema !== "openprose.prime-cleanup-marker/1"
    || typeof value.handle !== "string"
    || typeof value.directory !== "string"
    || value.socket !== "prime.sock"
    || (value.detectedVersion !== "0.7.0" && value.detectedVersion !== "0.8.1")) {
    throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
  }
  return value as unknown as RecoveryMarker;
}

async function validateRecoveryContents(directory: string): Promise<void> {
  let markerSeen = false;
  let entries = 0;
  const opened = await opendir(directory).catch(() => { throw cleanupFailure("recovery-directory-read"); });
  try {
    for (;;) {
      const entry = await opened.read().catch(() => { throw cleanupFailure("recovery-directory-read"); });
      if (entry === null) break;
      entries += 1;
      if (entries > MAX_RECOVERY_ENTRIES) throw cleanupFailure("recovery-entry-limit");
      if (entry.name === RECOVERY_MARKER) {
        markerSeen = true;
        continue;
      }
      if (entry.name === "prime.sock" && (await lstat(join(directory, entry.name))).isSocket()) continue;
      throw cleanupFailure("recovery-unexpected-entry");
    }
  } finally {
    try {
      await opened.close();
    } catch {
      // The stream may already have been closed by the runtime at EOF.
    }
  }
  if (!markerSeen) throw failure("CONFIG_INVALID", { reason: "Prime cleanup handle is not authentic" });
}

function isExpectedHello(value: unknown, socketPath: string, detectedVersion: string | null): boolean {
  if (!isRecord(value) || value.type !== "daemon_hello" || value.socketPath !== socketPath) return false;
  if (!isRecord(value.protocol) || value.protocol.name !== "prime-agent.daemon" || value.protocol.version !== 7) return false;
  return detectedVersion === null || value.appVersion === detectedVersion;
}

function isSuccessfulShutdown(value: unknown, id: string): boolean {
  return isRecord(value)
    && value.type === "response"
    && value.id === id
    && value.command === "shutdown"
    && value.success === true;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function connect(socketPath: string, timeoutMs: number): Promise<Socket | null> {
  return new Promise<Socket | null>((resolveSocket, reject) => {
    const socket = createConnection(socketPath);
    const timeout = setTimeout(() => finish(() => {
      socket.destroy();
      reject(cleanupFailure("daemon-connect-timeout"));
    }), timeoutMs);
    const finish = (callback: () => void) => {
      clearTimeout(timeout);
      socket.off("connect", connected);
      socket.off("error", errored);
      callback();
    };
    const connected = () => finish(() => resolveSocket(socket));
    const errored = (caught: NodeJS.ErrnoException) => finish(() => {
      socket.destroy();
      if (caught.code === "ENOENT") {
        void socketPathDisappears(socketPath, timeoutMs).then((absent) => {
          if (absent) resolveSocket(null);
          else reject(cleanupFailure("daemon-connect-indeterminate"));
        });
      } else reject(cleanupFailure(caught.code === "ECONNREFUSED" ? "daemon-connect-refused" : "daemon-connect"));
    });
    socket.once("connect", connected);
    socket.once("error", errored);
  });
}

async function readJsonLine(
  socket: Socket,
  buffered: { value: Buffer },
  policy: PrimeOwnedServicePolicy,
  phase: string,
): Promise<unknown> {
  return new Promise<unknown>((resolveFrame, reject) => {
    const finish = (callback: () => void) => {
      clearTimeout(timeout);
      socket.off("data", data);
      socket.off("error", errored);
      socket.off("close", closed);
      callback();
    };
    const consume = () => {
      const newline = buffered.value.indexOf(0x0a);
      if (newline < 0) {
        if (buffered.value.byteLength > policy.maximumFrameBytes) {
          finish(() => reject(cleanupFailure(`${phase}-limit`)));
        }
        return;
      }
      if (newline > policy.maximumFrameBytes) {
        finish(() => reject(cleanupFailure(`${phase}-limit`)));
        return;
      }
      const line = buffered.value.subarray(0, newline).toString("utf8");
      buffered.value = buffered.value.subarray(newline + 1);
      try {
        const parsed = JSON.parse(line) as unknown;
        finish(() => resolveFrame(parsed));
      } catch {
        finish(() => reject(cleanupFailure(`${phase}-malformed`)));
      }
    };
    const data = (chunk: Buffer) => {
      buffered.value = Buffer.concat([buffered.value, chunk]);
      consume();
    };
    const errored = () => finish(() => reject(cleanupFailure(`${phase}-io`)));
    const closed = () => finish(() => reject(cleanupFailure(`${phase}-closed`)));
    const timeout = setTimeout(() => finish(() => reject(cleanupFailure(`${phase}-timeout`))), policy.ioTimeoutMs);
    socket.on("data", data);
    socket.once("error", errored);
    socket.once("close", closed);
    consume();
  });
}

async function writeBounded(socket: Socket, value: string, timeoutMs: number): Promise<void> {
  await new Promise<void>((resolveWrite, reject) => {
    const finish = (callback: () => void) => {
      clearTimeout(timeout);
      socket.off("error", errored);
      callback();
    };
    const errored = () => finish(() => reject(cleanupFailure("shutdown-write")));
    const timeout = setTimeout(() => finish(() => reject(cleanupFailure("shutdown-write-timeout"))), timeoutMs);
    socket.once("error", errored);
    socket.write(value, (caught?: Error | null) => {
      finish(() => {
        if (caught) reject(cleanupFailure("shutdown-write"));
        else resolveWrite();
      });
    });
  });
}

async function probeListening(socketPath: string, timeoutMs: number): Promise<"listening" | "gone" | "indeterminate"> {
  return new Promise((resolveProbe) => {
    const socket = createConnection(socketPath);
    const timeout = setTimeout(() => finish("indeterminate"), timeoutMs);
    const finish = (result: "listening" | "gone" | "indeterminate") => {
      clearTimeout(timeout);
      socket.removeAllListeners();
      socket.destroy();
      resolveProbe(result);
    };
    socket.once("connect", () => finish("listening"));
    socket.once("error", (caught: NodeJS.ErrnoException) => {
      if (caught.code !== "ENOENT") {
        finish("indeterminate");
        return;
      }
      void socketPathDisappears(socketPath, timeoutMs).then((absent) => finish(absent ? "gone" : "indeterminate"));
    });
  });
}

async function socketPathDisappears(socketPath: string, timeoutMs: number): Promise<boolean> {
  const deadline = performance.now() + timeoutMs;
  while (true) {
    const absent = await lstat(socketPath).then(
      () => false,
      (caught: NodeJS.ErrnoException) => caught.code === "ENOENT",
    );
    if (absent) return true;
    const remaining = deadline - performance.now();
    if (remaining <= 0) return false;
    await Bun.sleep(Math.min(5, remaining));
  }
}
