import { constants } from "node:fs";
import { lstat, mkdir, mkdtemp, open, rmdir, unlink, type FileHandle } from "node:fs/promises";
import { basename, dirname, join, parse, resolve } from "node:path";
import { closed, PACKAGE_LIMITS, packagePath, parsePackageJSON, preparePackage, canonicalPackageJSON, type PreparedPackage, type PackageReceipt, invalidPackage } from "./package-format";
import type { PackageCommand } from "./package-args";

async function safeAncestors(path: string): Promise<void> {
  const absolute = resolve(path), root = parse(absolute).root;
  let current = root;
  for (const segment of absolute.slice(root.length).split("/").filter(Boolean)) {
    current = join(current, segment);
    const info = await lstat(current);
    if (!info.isDirectory() || info.isSymbolicLink()) invalidPackage();
  }
}
async function boundedRegularFile(path: string, limit: number): Promise<Uint8Array> {
  await safeAncestors(dirname(path));
  const before = await lstat(path);
  if (!before.isFile() || before.isSymbolicLink() || before.size > limit) return invalidPackage();
  let handle: FileHandle | undefined;
  try {
    handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    const opened = await handle.stat();
    if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino || opened.size > limit) return invalidPackage();
    const buffer = Buffer.alloc(limit + 1);
    let offset = 0;
    while (offset < buffer.length) {
      const read = await handle.read(buffer, offset, buffer.length - offset, offset);
      if (!read.bytesRead) break;
      offset += read.bytesRead;
    }
    const after = await handle.stat();
    if (offset > limit || after.size !== opened.size || after.mtimeMs !== opened.mtimeMs || after.ctimeMs !== opened.ctimeMs) return invalidPackage();
    await safeAncestors(dirname(path));
    return buffer.subarray(0, offset);
  } finally { await handle?.close(); }
}
// Directory manifests are authored locally: unlike network JSON, duplicate
// member names must never silently replace the user's explicit file selection.
export function parseDirectoryManifest(bytes: Uint8Array): unknown {
  const parsed = parsePackageJSON(bytes);
  const source = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  let index = 0;
  const whitespace = () => { while (/\s/u.test(source[index] ?? "") && index < source.length) index++; };
  const string = (): string => {
    const start = index++;
    while (index < source.length) {
      const character = source[index++];
      if (character === "\\") index++;
      else if (character === '\"') return JSON.parse(source.slice(start, index)) as string;
    }
    return invalidPackage();
  };
  const value = (depth: number): void => {
    if (depth > 128) invalidPackage();
    whitespace();
    if (source[index] === '\"') { string(); return; }
    if (source[index] === "{") {
      index++; whitespace();
      const names = new Set<string>();
      if (source[index] === "}") { index++; return; }
      for (;;) {
        whitespace();
        const name = string();
        if (names.has(name)) invalidPackage();
        names.add(name); whitespace(); index++;
        value(depth + 1); whitespace();
        if (source[index++] === "}") return;
      }
    }
    if (source[index] === "[") {
      index++; whitespace();
      if (source[index] === "]") { index++; return; }
      for (;;) { value(depth + 1); whitespace(); if (source[index++] === "]") return; }
    }
    while (index < source.length && !/[\s,}\]]/u.test(source[index]!)) index++;
  };
  value(0);
  return parsed;
}
export async function prepareSource(command: PackageCommand, cwd: string): Promise<PreparedPackage> {
  const source = resolve(cwd, command.input);
  await safeAncestors(dirname(source));
  const info = await lstat(source);
  if (info.isSymbolicLink()) return invalidPackage();
  let paths: string[], exports: unknown, dependencies: unknown;
  if (info.isFile()) {
    paths = [packagePath(basename(source))]; exports = { default: paths[0] }; dependencies = {};
  } else if (info.isDirectory()) {
    const manifest = closed(parseDirectoryManifest(await boundedRegularFile(join(source, "prose-package.json"), PACKAGE_LIMITS.request)), ["schema", "files", "exports", "dependencies"]);
    if (manifest.schema !== "prose-package-directory-v1" || !Array.isArray(manifest.files) || !manifest.files.length || manifest.files.length > PACKAGE_LIMITS.files) return invalidPackage();
    paths = manifest.files.map(packagePath); exports = manifest.exports; dependencies = manifest.dependencies;
  } else return invalidPackage();
  const files = [];
  let total = 0;
  for (const path of paths) {
    const bytes = await boundedRegularFile(info.isFile() ? source : join(source, path), PACKAGE_LIMITS.file);
    total += bytes.length;
    if (total > PACKAGE_LIMITS.total) return invalidPackage();
    files.push({ path, encoding: "base64", content: Buffer.from(bytes).toString("base64") });
  }
  return preparePackage({ schema: "prose-package-v1", manifest: { organization: command.organization, package: command.name, version: command.version, visibility: command.public ? "public" : "private", exports, dependencies }, files });
}
interface Owned { path: string; dev: number; ino: number; directory: boolean }
async function identity(path: string, directory: boolean): Promise<Owned> {
  const info = await lstat(path);
  if (info.isSymbolicLink() || (directory ? !info.isDirectory() : !info.isFile())) return invalidPackage();
  return { path, dev: info.dev, ino: info.ino, directory };
}
async function stillOwned(owned: Owned): Promise<boolean> {
  try { const info = await lstat(owned.path); return info.dev === owned.dev && info.ino === owned.ino && !info.isSymbolicLink(); } catch { return false; }
}
async function cleanupOwned(entries: Owned[]): Promise<void> {
  for (const entry of [...entries].reverse()) {
    const ancestors = entries.filter(other => other.directory && entry.path.startsWith(`${other.path}/`));
    if (!(await stillOwned(entry)) || (await Promise.all(ancestors.map(stillOwned))).some(owned => !owned)) continue;
    try { if (entry.directory) await rmdir(entry.path); else await unlink(entry.path); } catch { /* Preserve concurrent additions and substitutions. */ }
  }
}
export async function materializePackage(prepared: PreparedPackage, receipt: PackageReceipt, outputDir: string, cwd: string): Promise<void> {
  if (!outputDir || outputDir.includes("\0")) return invalidPackage();
  const destination = resolve(cwd, outputDir), parent = dirname(destination);
  await safeAncestors(parent);
  try { await lstat(destination); return invalidPackage(); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
  const staging = await mkdtemp(join(parent, ".prose-package-"));
  const staged: Owned[] = [await identity(staging, true)];
  const reserved: Owned[] = [];
  const paths = [...prepared.files.map(file => ({ path: file.path, bytes: file.bytes })), { path: ".prose-package-receipt.json", bytes: new TextEncoder().encode(`${canonicalPackageJSON(receipt)}\n`) }];
  const writeOwned = async (root: string, entries: Owned[], path: string, bytes: Uint8Array) => {
    let directory = root;
    const segments = path.split("/");
    for (const part of segments.slice(0, -1)) {
      directory = join(directory, part);
      const known = entries.find(entry => entry.path === directory);
      if (known !== undefined) { if (!(await stillOwned(known))) return invalidPackage(); }
      else { await mkdir(directory, { mode: 0o700 }); entries.push(await identity(directory, true)); }
    }
    // Authenticate each owned ancestor immediately before the exclusive create.
    for (const entry of entries.filter(entry => entry.directory)) if (!(await stillOwned(entry))) return invalidPackage();
    await safeAncestors(directory);
    const target = join(root, path);
    const handle = await open(target, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try {
      const info = await handle.stat();
      entries.push({ path: target, dev: info.dev, ino: info.ino, directory: false });
      await handle.writeFile(bytes);
    } finally { await handle.close(); }
  };
  try {
    for (const file of paths) await writeOwned(staging, staged, file.path, file.bytes);
    await safeAncestors(parent);
    // Exclusive reservation never replaces even an empty destination. Visibility is
    // deliberately non-atomic on hosts lacking an exposed NOREPLACE directory rename.
    await mkdir(destination, { mode: 0o700 });
    reserved.push(await identity(destination, true));
    for (const file of paths) await writeOwned(destination, reserved, file.path, file.bytes);
    for (const entry of reserved) if (!(await stillOwned(entry))) return invalidPackage();
  } catch (error) {
    await cleanupOwned(reserved);
    throw error;
  } finally { await cleanupOwned(staged); }
}
