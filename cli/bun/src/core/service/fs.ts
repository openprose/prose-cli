import { quote } from "../output";
// Local file helpers for service operations: bounded source reads (a path or
// `-` for standard input), never-overwrite output files, and the
// fresh-directory writer used by downloads.
import { closeSync, lstatSync, mkdirSync, openSync, readFileSync, realpathSync, statSync, writeSync, fsyncSync } from "node:fs";
import { dirname, isAbsolute, join, sep } from "node:path";
import { failure, invocationFailure } from "../errors";
import { RunnerFailure } from "../types";

/**
 * Joins without lexical normalization so the OS resolves `..`, `.` and a
 * trailing `/` exactly as Rust's `Path::join` + open does.
 */
function absolute(cwd: string, value: string): string {
  if (isAbsolute(value)) return value;
  return cwd.endsWith(sep) ? cwd + value : cwd + sep + value;
}

function exists(path: string): boolean {
  try { lstatSync(path); return true; } catch { return false; }
}

/** Reads at most `max` bytes from a path relative to `cwd`, or `-` for stdin. */
export async function readSource(cwd: string, value: string, max: number, label: string): Promise<Uint8Array> {
  let bytes: Uint8Array;
  if (value === "-") {
    const chunks: Uint8Array[] = [];
    let length = 0;
    try {
      for await (const chunk of Bun.stdin.stream()) {
        length += chunk.length;
        if (length > max) throw invocationFailure(`${label} ${quote(value)} is larger than ${max} bytes`);
        chunks.push(chunk);
      }
    } catch (caught) {
      if (caught instanceof RunnerFailure) throw caught;
      throw invocationFailure(`cannot read ${label} ${quote(value)}`);
    }
    bytes = Buffer.concat(chunks);
  } else {
    const path = absolute(cwd, value);
    let isFile = false;
    try { isFile = statSync(path).isFile(); } catch { isFile = false; }
    if (!isFile) throw invocationFailure(`${label} ${quote(value)} is not a readable file`);
    try {
      if (statSync(path).size > max) throw invocationFailure(`${label} ${quote(value)} is larger than ${max} bytes`);
      bytes = readFileSync(path);
    } catch (caught) {
      if (caught instanceof RunnerFailure) throw caught;
      throw invocationFailure(`cannot read ${label} ${quote(value)}`);
    }
  }
  if (bytes.length > max) throw invocationFailure(`${label} ${quote(value)} is larger than ${max} bytes`);
  return bytes;
}

/** Like readSource, and the bytes must be UTF-8 text. */
export async function readText(cwd: string, value: string, max: number, label: string): Promise<string> {
  const bytes = await readSource(cwd, value, max, label);
  // ignoreBOM: a leading U+FEFF is content; Rust's String::from_utf8 keeps it too.
  try { return new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes); }
  catch { throw invocationFailure(`${label} ${quote(value)} is not UTF-8 text`); }
}

/**
 * Checks before any request that `value` can be created as a new file: it must
 * name a file (non-empty, no trailing `/`, final segment not `.` or `..`), must
 * not exist, and its parent directory must. Returns the unnormalized path.
 */
export function checkNewFile(cwd: string, value: string): string {
  const last = value.slice(value.lastIndexOf("/") + 1);
  if (last === "" || last === "." || last === "..") throw invocationFailure(`output file ${quote(value)} must name a file, not a directory`);
  const path = absolute(cwd, value);
  if (exists(path)) throw invocationFailure(`output file ${quote(value)} already exists; choose a new path`);
  let parentIsDirectory = false;
  try { parentIsDirectory = statSync(dirname(path)).isDirectory(); } catch { parentIsDirectory = false; }
  if (!parentIsDirectory) throw invocationFailure(`the directory for output file ${quote(value)} does not exist`);
  return path;
}

/** Opens `value` for writing after checkNewFile. */
export function createNewFile(cwd: string, value: string): number {
  const path = checkNewFile(cwd, value);
  try { return openSync(path, "wx"); } catch { throw invocationFailure(`cannot create output file ${quote(value)}`); }
}

/** Writes bytes to a new file (see createNewFile). */
export function writeNewFile(cwd: string, value: string, bytes: Uint8Array): void {
  const descriptor = createNewFile(cwd, value);
  try { writeSync(descriptor, bytes); fsyncSync(descriptor); }
  catch { throw invocationFailure(`cannot write output file ${quote(value)}`); }
  finally { closeSync(descriptor); }
}

/** A service-supplied relative path: no absolute path, backslash, dot segment, empty segment or control character (and on Windows no colon); at most 1024 bytes. */
export function validRelativePath(value: string, platform: NodeJS.Platform = process.platform): boolean {
  return value.length > 0
    // Windows: a colon names a drive or an alternate data stream (Rust's
    // Normal-component check rejects the same paths there).
    && !(platform === "win32" && value.includes(":"))
    && new TextEncoder().encode(value).length <= 1024
    && !value.startsWith("/")
    && !value.includes("\\")
    && !/[\u0000-\u001f\u007f]/u.test(value)
    && value.split("/").every((part) => part.length > 0 && part !== "." && part !== "..");
}

/** A directory created by this invocation; files are created inside it only. */
export class FreshDirectory {
  private constructor(readonly root: string) {}

  static create(cwd: string, value: string): FreshDirectory {
    const root = absolute(cwd, value);
    if (exists(root)) throw invocationFailure(`output directory ${quote(value)} already exists; choose a new directory`);
    let parentIsDirectory = false;
    try { parentIsDirectory = statSync(dirname(root)).isDirectory(); } catch { parentIsDirectory = false; }
    if (!parentIsDirectory) throw invocationFailure(`the parent of output directory ${quote(value)} does not exist`);
    try { mkdirSync(root); } catch { throw invocationFailure(`cannot create output directory ${quote(value)}`); }
    return new FreshDirectory(root);
  }

  /** Opens `relative` inside the directory for writing (never overwriting). */
  createFile(relative: string): number {
    if (!validRelativePath(relative)) throw failure("SERVICE_PROTOCOL_INVALID", { reason: "the service named an unsafe output path" });
    const path = join(this.root, relative);
    const parent = dirname(path);
    try { mkdirSync(parent, { recursive: true }); } catch { throw invocationFailure("cannot create an output subdirectory"); }
    let canonicalParent: string;
    try { canonicalParent = realpathSync(parent); } catch { throw invocationFailure("cannot resolve an output subdirectory"); }
    let canonicalRoot: string;
    try { canonicalRoot = realpathSync(this.root); } catch { throw invocationFailure("cannot resolve the output directory"); }
    if (canonicalParent !== canonicalRoot && !canonicalParent.startsWith(canonicalRoot + sep)) throw invocationFailure("an output path escapes the output directory");
    try { return openSync(path, "wx"); } catch { throw invocationFailure(`cannot create output file ${quote(relative)}`); }
  }
}

