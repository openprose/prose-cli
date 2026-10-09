import policy from "../../../shared/image/kernel-startup/policy.json" with { type: "json" };
import template from "../../../shared/image/kernel-startup/manifest.template.json" with { type: "json" };
import task from "../../../shared/image/kernel-startup/contracts/task-envelope.schema.json" with { type: "text" };
import terminal from "../../../shared/image/kernel-startup/contracts/terminal.schema.json" with { type: "text" };
import framing from "../../../shared/image/kernel-startup/contracts/framing.txt" with { type: "text" };
import { failure, kernelRetrievalFailure, type KernelRetrievalStage } from "./errors";
import { sha256, verifyRuntimeImage } from "./image";
import { RunnerFailure, type RuntimeImageManifest, type VerifiedRuntimeImage } from "./types";

export interface KernelResponse { status: number; location?: string | undefined; bytes: Uint8Array }
export type KernelGet = (url: string, limit: number, signal: AbortSignal) => Promise<KernelResponse>;
const encoder = new TextEncoder();
const invalid = (reason: string) => failure("IMAGE_INVALID", { reason });

async function httpGet(url: string, limit: number, signal: AbortSignal): Promise<KernelResponse> {
  const response = await fetch(url, { redirect: "manual", signal, headers: { "User-Agent": policy.userAgent } });
  const location = response.headers.get("location") ?? undefined;
  if (response.status !== 200) {
    // Discarded response cleanup must not mask the observed HTTP status.
    try { await response.body?.cancel(); } catch { /* No response bytes are consumed. */ }
    return { status: response.status, location, bytes: new Uint8Array() };
  }
  const chunks: Uint8Array[] = [];
  let length = 0;
  const reader = response.body?.getReader();
  if (!reader) throw invalid("Published kernel response has no body.");
  let failed = false;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      length += value.length;
      if (length > limit) throw failure("IMAGE_TOO_LARGE", { maximumBytes: limit });
      chunks.push(value);
    }
  } catch (error) {
    failed = true;
    throw error;
  } finally {
    try { await reader.cancel(); } catch (error) { if (!failed) throw error; }
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.length; }
  return { status: response.status, location, bytes };
}

/** Acquire only the kernel entry, never install packages or interpret Markdown. */
export async function publishedKernel(get: KernelGet = httpGet, cancellation?: AbortSignal): Promise<VerifiedRuntimeImage> {
  const timeout = AbortSignal.timeout(policy.timeoutMs);
  const signal = cancellation ? AbortSignal.any([timeout, cancellation]) : timeout;
  try {
    const request = async (url: string, limit: number, stage: KernelRetrievalStage) => {
      try { return await get(url, limit, signal); }
      catch (error) {
        if (error instanceof RunnerFailure && ["IMAGE_INVALID", "IMAGE_TOO_LARGE", "CANCELLED", "STARTUP_TIMEOUT"].includes(error.code)) throw error;
        throw kernelRetrievalFailure(stage);
      }
    };
    const entry = await request(policy.entry, policy.maxMetadataBytes, "entry");
    if (entry.status >= 400 && entry.status <= 599) throw kernelRetrievalFailure("entry", entry.status);
    if (![301, 302, 303, 307, 308].includes(entry.status) || !entry.location) throw invalid("Kernel entry must redirect to an immutable release.");
    const location = entry.location.startsWith("/") && !entry.location.startsWith("//") ? policy.origin + entry.location : entry.location;
    if (!location.startsWith(policy.origin + "/releases/") || /[%?#]/.test(location)) throw invalid("Kernel entry selected an unsupported release URL.");
    const url = new URL(location);
    const match = /^\/releases\/([A-Za-z0-9][A-Za-z0-9._-]{0,127})\/core\/README\.md$/.exec(url.pathname);
    if (url.origin !== policy.origin || url.username || url.password || url.search || url.hash || !match) throw invalid("Kernel entry selected an unsupported release URL.");
    const release = match[1]!;
    const root = url.href.slice(0, -"README.md".length);
    const read = async (url: string, limit: number, stage: KernelRetrievalStage) => {
      const r = await request(url, limit, stage);
      if (r.status !== 200) throw kernelRetrievalFailure(stage, r.status);
      if (r.bytes.length > limit) throw failure("IMAGE_TOO_LARGE", { maximumBytes: limit });
      return r.bytes;
    };
    const json = (bytes: Uint8Array) => JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    const descriptor = json(await read(root + "descriptor.json", policy.maxMetadataBytes, "descriptor"));
    if (descriptor.identity !== "openprose/core" || descriptor.release !== release || descriptor.exports?.entry !== "README.md" || descriptor.inventory !== `releases/${release}/core/inventory.json` || !/^[a-f0-9]{40}$/.test(descriptor.source?.commit ?? "") || !/^[a-f0-9]{64}$/.test(descriptor.inventory_sha256 ?? "")) throw invalid("Published kernel descriptor identity is invalid.");
    const inventoryBytes = await read(root + "inventory.json", policy.maxMetadataBytes, "inventory");
    if (await sha256(inventoryBytes) !== descriptor.inventory_sha256) throw invalid("Published kernel inventory digest mismatch.");
    const expected = json(inventoryBytes)["README.md"];
    if (!expected || expected.mode !== "100644" || !/^[a-f0-9]{64}$/.test(expected.sha256 ?? "")) throw invalid("Published kernel inventory entry is invalid.");
    const kernel = await read(url.href, policy.maxKernelBytes, "kernel");
    if (!kernel.length || await sha256(kernel) !== expected.sha256) throw invalid("Published kernel content digest mismatch.");
    const manifest = structuredClone(template) as RuntimeImageManifest;
    manifest.imageVersion = `kernel-${release}`;
    manifest.languageVersion = release;
    manifest.semanticSourceRevision = descriptor.source.commit;
    manifest.payload[0]!.byteLength = kernel.length;
    manifest.payload[0]!.sha256 = expected.sha256;
    manifest.modelVisibleBytes.byteLength = kernel.length;
    manifest.modelVisibleBytes.sha256 = expected.sha256;
    const prefix = encoder.encode(`payload/kernel.md\0${kernel.length}\0`);
    const aggregate = new Uint8Array(prefix.length + kernel.length + 1);
    aggregate.set(prefix); aggregate.set(kernel, prefix.length);
    manifest.aggregateSha256.sha256 = await sha256(aggregate);
    const files = new Map<string, Uint8Array>([["payload/kernel.md", kernel], ["contracts/task-envelope.schema.json", encoder.encode(task as unknown as string)], ["contracts/terminal.schema.json", encoder.encode(terminal as unknown as string)], ["contracts/framing.txt", encoder.encode(framing)]]);
    return await verifyRuntimeImage({ manifest, files });
  } catch (error) {
    if (signal.aborted) throw failure(cancellation?.aborted ? "CANCELLED" : "STARTUP_TIMEOUT", { reason: "Kernel retrieval was cancelled or exceeded its startup deadline." });
    if (error instanceof RunnerFailure) throw error;
    throw invalid("Cannot retrieve or verify the published kernel; no fallback was used.");
  }
}
