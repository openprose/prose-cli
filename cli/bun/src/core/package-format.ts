import { createHash } from "node:crypto";
export const PACKAGE_LIMITS = { request: 2 * 1024 * 1024, file: 256 * 1024, total: 1024 * 1024, files: 128 };
type RecordValue = Record<string, unknown>;
export interface PackageReference { organization: string; package: string; version: string; sha256: string }
export interface InventoryEntry { path: string; size: number; sha256: string }
export interface PackageReceipt { schema: "prose-publication-v1"; organizationId: string; reference: PackageReference; visibility: "private" | "public"; inventory: InventoryEntry[] }
export interface PreparedPackage { artifact: unknown; bytes: Uint8Array; reference: PackageReference; inventory: InventoryEntry[]; visibility: "private" | "public"; files: Array<{ path: string; bytes: Uint8Array }> }
export function invalidPackage(): never { throw new Error("Invalid package data"); }
export function record(value: unknown): RecordValue {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return invalidPackage();
  return value as RecordValue;
}
export function closed(value: unknown, keys: string[], required = keys): RecordValue {
  const object = record(value);
  if (Object.keys(object).some(key => !keys.includes(key)) || required.some(key => !Object.hasOwn(object, key))) return invalidPackage();
  return object;
}
export function canonicalPackageJSON(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalPackageJSON).join(",")}]`;
  if (value !== null && typeof value === "object") return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonicalPackageJSON((value as RecordValue)[key])}`).join(",")}}`;
  if (value === null || typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isSafeInteger(value) && value >= 0)) return JSON.stringify(value);
  return invalidPackage();
}
export function packageHash(bytes: Uint8Array): string { return createHash("sha256").update(bytes).digest("hex"); }
export function identity(value: unknown): string {
  if (typeof value !== "string" || /\s/u.test(value) || !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/u.test(value)) return invalidPackage();
  return value;
}
export function version(value: unknown): string {
  if (typeof value !== "string" || value.length > 128 || /\s/u.test(value) || !/^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$/u.test(value)) return invalidPackage();
  const pre = value.split("+")[0]!.split("-").slice(1).join("-");
  if (pre.split(".").some(part => /^0[0-9]+$/u.test(part))) return invalidPackage();
  return value;
}
export function digest(value: unknown): string {
  if (typeof value !== "string" || value.length !== 64 || !/^[0-9a-f]{64}$/u.test(value)) return invalidPackage();
  return value;
}
export function packagePath(value: unknown): string {
  if (typeof value !== "string" || value.length > 240 || /\s/u.test(value) || !/^[A-Za-z0-9_][A-Za-z0-9._/-]*$/u.test(value)) return invalidPackage();
  if (value.split("/").some(part => !part || part === "." || part === ".." || part.startsWith(".") || part.endsWith(".") || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/iu.test(part) || /^(node_modules|credentials|secrets?)(?:\.|$)/iu.test(part) || /\.(pem|key|p12|pfx)$/iu.test(part))) return invalidPackage();
  return value;
}
export function reference(value: unknown): PackageReference {
  const ref = closed(value, ["organization", "package", "version", "sha256"]);
  return { organization: identity(ref.organization), package: identity(ref.package), version: version(ref.version), sha256: digest(ref.sha256) };
}
function noCollisions(paths: string[]): void {
  const folded = paths.map(path => path.toLowerCase());
  if (new Set(folded).size !== paths.length || folded.some(path => folded.some(other => other.startsWith(`${path}/`)))) invalidPackage();
}
export function parsePackageJSON(bytes: Uint8Array): unknown {
  if (bytes.length > PACKAGE_LIMITS.request) return invalidPackage();
  return JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
}
export function preparePackage(input: unknown): PreparedPackage {
  const request = closed(input, ["schema", "manifest", "files"]);
  if (request.schema !== "prose-package-v1" || Buffer.byteLength(canonicalPackageJSON(input)) > PACKAGE_LIMITS.request) return invalidPackage();
  const manifest = closed(request.manifest, ["organization", "package", "version", "visibility", "exports", "dependencies"], ["organization", "package", "version", "exports", "dependencies"]);
  const organization = identity(manifest.organization), name = identity(manifest.package), release = version(manifest.version);
  const visibility = Object.hasOwn(manifest, "visibility") ? manifest.visibility : "private";
  if (visibility !== "public" && visibility !== "private") return invalidPackage();
  const exports = record(manifest.exports), dependencies = record(manifest.dependencies);
  if (!Object.keys(exports).length || Object.keys(exports).length > 64 || Object.keys(dependencies).length > 64) return invalidPackage();
  for (const [alias, ref] of Object.entries(dependencies)) { identity(alias); reference(ref); }
  if (!Array.isArray(request.files) || !request.files.length || request.files.length > PACKAGE_LIMITS.files) return invalidPackage();
  let total = 0;
  const files = request.files.map(raw => {
    const file = closed(raw, ["path", "encoding", "content"]), path = packagePath(file.path);
    if (typeof file.content !== "string" || file.content.length > PACKAGE_LIMITS.request) return invalidPackage();
    let bytes: Uint8Array;
    if (file.encoding === "utf8") {
      if (/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/u.test(file.content)) return invalidPackage();
      bytes = new TextEncoder().encode(file.content);
    } else {
      if (file.encoding !== "base64" || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u.test(file.content)) return invalidPackage();
      bytes = Buffer.from(file.content, "base64");
      if (Buffer.from(bytes).toString("base64") !== file.content) return invalidPackage();
    }
    total += bytes.length;
    if (bytes.length > PACKAGE_LIMITS.file || total > PACKAGE_LIMITS.total) return invalidPackage();
    return { path, bytes };
  }).sort((a, b) => a.path < b.path ? -1 : a.path > b.path ? 1 : 0);
  noCollisions(files.map(file => file.path));
  for (const [name, path] of Object.entries(exports)) {
    if (/\s/u.test(name) || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/u.test(name) || ["__proto__", "prototype", "constructor"].includes(name)) return invalidPackage();
    packagePath(path);
    if (!files.some(file => file.path === path)) return invalidPackage();
  }
  const artifact = { schema: "prose-package-v1", manifest: { ...manifest, visibility }, files: files.map(file => ({ path: file.path, encoding: "base64", content: Buffer.from(file.bytes).toString("base64") })) };
  const bytes = new TextEncoder().encode(`${canonicalPackageJSON(artifact)}\n`);
  if (bytes.length > PACKAGE_LIMITS.request) return invalidPackage();
  return { artifact, bytes, reference: { organization, package: name, version: release, sha256: packageHash(bytes) }, visibility, files, inventory: files.map(file => ({ path: file.path, size: file.bytes.length, sha256: packageHash(file.bytes) })) };
}
export function receipt(value: unknown): PackageReceipt {
  const result = closed(value, ["schema", "organizationId", "reference", "visibility", "inventory"]);
  if (result.schema !== "prose-publication-v1" || typeof result.organizationId !== "string" || result.organizationId.length !== 36 || !/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/u.test(result.organizationId) || (result.visibility !== "public" && result.visibility !== "private")) return invalidPackage();
  const ref = reference(result.reference);
  if (!Array.isArray(result.inventory) || !result.inventory.length || result.inventory.length > PACKAGE_LIMITS.files) return invalidPackage();
  let total = 0, previous = "";
  const inventory = result.inventory.map(raw => {
    const entry = closed(raw, ["path", "size", "sha256"]), path = packagePath(entry.path);
    if (path <= previous || typeof entry.size !== "number" || !Number.isSafeInteger(entry.size) || entry.size < 0 || entry.size > PACKAGE_LIMITS.file) return invalidPackage();
    total += entry.size; previous = path;
    if (total > PACKAGE_LIMITS.total) return invalidPackage();
    return { path, size: entry.size, sha256: digest(entry.sha256) };
  });
  noCollisions(inventory.map(entry => entry.path));
  return { schema: "prose-publication-v1", organizationId: result.organizationId, reference: ref, visibility: result.visibility, inventory };
}
export function matchReceipt(value: PackageReceipt, prepared: PreparedPackage): void {
  if (canonicalPackageJSON(value.reference) !== canonicalPackageJSON(prepared.reference) || value.visibility !== prepared.visibility || canonicalPackageJSON(value.inventory) !== canonicalPackageJSON(prepared.inventory)) invalidPackage();
}
