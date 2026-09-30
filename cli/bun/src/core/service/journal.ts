// The local run journal. Same layout as the Rust product:
// $XDG_STATE_HOME/openprose/cli/production/runs/<session>.json (a dev build
// with a custom endpoint uses cli/custom-<origin digest>/ instead), directories
// 0700, files 0600, entries {session, runId, createdAt, lastSequence,
// sourceSha256} and never a credential; entries older than 30 days are pruned.
import { chmodSync, mkdirSync, openSync, readFileSync, readdirSync, renameSync, rmSync, writeSync, closeSync, fsyncSync } from "node:fs";
import { isAbsolute, join } from "node:path";
import { failure } from "../errors";
import { journalComponent, type Environment, type Json, type JsonObject } from "./manifest";
import { canonicalJson } from "./render";

export interface JournalEntry {
  session: string;
  runId: string | null;
  createdAt: string;
  lastSequence: number;
  sourceSha256: string | null;
}

export function validSession(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(value);
}

export function entryFromJson(value: Json): JournalEntry | undefined {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return undefined;
  const object = value as JsonObject;
  if (typeof object.session !== "string" || !validSession(object.session)) return undefined;
  if (!(object.runId === null || typeof object.runId === "string")) return undefined;
  if (typeof object.createdAt !== "string") return undefined;
  if (typeof object.lastSequence !== "number" || !Number.isSafeInteger(object.lastSequence) || object.lastSequence < 0) return undefined;
  if (!(object.sourceSha256 === null || typeof object.sourceSha256 === "string")) return undefined;
  return { session: object.session, runId: object.runId, createdAt: object.createdAt, lastSequence: object.lastSequence, sourceSha256: object.sourceSha256 };
}

/** An entry's bytes: strict UTF-8 (an entry that is not is skipped, as in Rust) and JSON. */
function entryFromBytes(bytes: Uint8Array): JournalEntry | undefined {
  try { return entryFromJson(JSON.parse(new TextDecoder("utf-8", { fatal: true, ignoreBOM: true }).decode(bytes)) as Json); }
  catch { return undefined; }
}

/** Milliseconds of an RFC 3339 date-time (`T`, `t` or a space; `Z`, `z` or an offset), or undefined. */
export function rfc3339(value: string): number | undefined {
  const match = /^(\d{4})-(\d{2})-(\d{2})[Tt ](\d{2}):(\d{2}):(\d{2})(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$/u.exec(value);
  if (match === null) return undefined;
  const [year, month, day, hour, minute, second] = match.slice(1, 7).map(Number) as [number, number, number, number, number, number];
  const days = [31, year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0) ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  // A leap second (:60) counts as the next second, as chrono reads it.
  if (month < 1 || month > 12 || day < 1 || day > days[month - 1]! || hour > 23 || minute > 59 || second > 60) return undefined;
  const zone = match[8]!;
  if (zone.length > 1 && (Number(zone.slice(1, 3)) > 23 || Number(zone.slice(4, 6)) > 59)) return undefined;
  const offset = zone.toUpperCase() === "Z" ? 0 : (zone.startsWith("-") ? -1 : 1) * (Number(zone.slice(1, 3)) * 60 + Number(zone.slice(4, 6)));
  const fraction = match[7] === undefined ? 0 : Number(`0${match[7]}`) * 1000;
  return Date.UTC(year, month - 1, day, hour, minute, second) + fraction - offset * 60_000;
}

function journalFailure(reason: string) {
  return failure("CONFIG_INVALID", { reason: `run journal: ${reason}` });
}

export class Journal {
  constructor(readonly root: string | undefined, readonly directory: string | undefined) {}

  static forEnvironment(env: Readonly<Record<string, string | undefined>>, homeDir: string | undefined, platform: NodeJS.Platform, environment: Environment): Journal {
    const xdg = env.XDG_STATE_HOME;
    let state: string | undefined;
    const localAppData = env.LOCALAPPDATA;
    if (xdg !== undefined && isAbsolute(xdg)) state = xdg;
    // Windows: LOCALAPPDATA first, whether or not a home directory is known.
    else if (platform === "win32" && localAppData !== undefined && localAppData !== "") state = localAppData;
    else if (homeDir !== undefined) {
      state = platform === "darwin" ? join(homeDir, "Library", "Application Support")
        : platform === "win32" ? join(homeDir, "AppData", "Local")
        : join(homeDir, ".local", "state");
    }
    const root = state === undefined ? undefined : join(state, "openprose");
    return new Journal(root, root === undefined ? undefined : join(root, "cli", journalComponent(environment), "runs"));
  }

  private ensure(): string {
    if (this.root === undefined || this.directory === undefined) throw journalFailure("no state directory (set HOME or XDG_STATE_HOME)");
    try { mkdirSync(this.directory, { recursive: true }); } catch { throw journalFailure("cannot create the journal directory"); }
    let current = this.directory;
    for (;;) {
      try { if (process.platform !== "win32") chmodSync(current, 0o700); } catch { throw journalFailure("cannot make the journal directory private"); }
      if (current === this.root) break;
      const parent = join(current, "..");
      if (parent === current) break;
      current = parent;
    }
    return this.directory;
  }

  private path(session: string): string {
    if (!validSession(session)) throw journalFailure("invalid session id");
    if (this.directory === undefined) throw journalFailure("no state directory (set HOME or XDG_STATE_HOME)");
    return join(this.directory, `${session}.json`);
  }

  /** Writes (creates or replaces) an entry atomically with mode 0600. */
  write(entry: JournalEntry): void {
    const directory = this.ensure();
    const path = this.path(entry.session);
    const temporary = join(directory, `.${entry.session}.tmp`);
    // Sorted keys: byte-identical to the Rust product.
    const text = `${canonicalJson({ session: entry.session, runId: entry.runId, createdAt: entry.createdAt, lastSequence: entry.lastSequence, sourceSha256: entry.sourceSha256 })}\n`;
    rmSync(temporary, { force: true });
    let descriptor: number | undefined;
    try {
      descriptor = openSync(temporary, "wx", 0o600);
      writeSync(descriptor, text);
      fsyncSync(descriptor);
    } catch { throw journalFailure("cannot write a journal entry"); }
    finally { if (descriptor !== undefined) closeSync(descriptor); }
    try { renameSync(temporary, path); } catch { throw journalFailure("cannot replace a journal entry"); }
  }

  writeJson(value: Json): void {
    const entry = entryFromJson(value);
    if (entry === undefined) throw journalFailure("invalid preset entry");
    this.write(entry);
  }

  get(session: string): JournalEntry | undefined {
    const path = this.path(session);
    let bytes: Uint8Array;
    try { bytes = readFileSync(path); }
    catch (caught) {
      if ((caught as NodeJS.ErrnoException).code === "ENOENT") return undefined;
      throw journalFailure("cannot read a journal entry");
    }
    return entryFromBytes(bytes);
  }

  /** Every readable entry, newest createdAt first (ties by session). */
  entries(): JournalEntry[] {
    if (this.directory === undefined) return [];
    let names: string[];
    try { names = readdirSync(this.directory); } catch { return []; }
    const entries: JournalEntry[] = [];
    for (const name of names.filter((item) => item.endsWith(".json"))) {
      try {
        const entry = entryFromBytes(readFileSync(join(this.directory, name)));
        if (entry !== undefined) entries.push(entry);
      } catch { /* unreadable entries are ignored */ }
    }
    return entries.sort((left, right) => (right.createdAt < left.createdAt ? -1 : right.createdAt > left.createdAt ? 1 : left.session < right.session ? -1 : left.session > right.session ? 1 : 0));
  }

  findRun(runId: string): JournalEntry | undefined {
    return this.entries().find((entry) => entry.runId === runId);
  }

  /** Removes entries created more than `days` before `now`; only RFC 3339 times count (as in Rust). */
  prune(now: string, days: number): number {
    const reference = rfc3339(now);
    if (reference === undefined) return 0;
    const cutoff = reference - days * 86_400_000;
    let removed = 0;
    for (const entry of this.entries()) {
      const created = rfc3339(entry.createdAt);
      if (created !== undefined && created < cutoff) {
        try { rmSync(this.path(entry.session)); removed += 1; } catch { /* keep */ }
      }
    }
    return removed;
  }
}
