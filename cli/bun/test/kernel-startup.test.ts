import { expect, test } from "bun:test";
import fixture from "../../shared/fixtures/kernel-startup/release.json";
import { publishedKernel, type KernelGet } from "../src/core/kernel-startup";
const root = "https://pkg.prose.md/releases/fixture-1/core/";
const entry = "https://pkg.prose.md/kernel.md";
type Response = { status: number; location?: string; text: string };
function setup(change?: (responses: Record<string, Response>) => void) {
  const responses: Record<string, Response> = structuredClone(fixture.responses);
  change?.(responses);
  const calls: string[] = [];
  const get: KernelGet = async (url) => { calls.push(url); const r = responses[url]; if (!r) throw Error("Unexpected URL"); return { ...r, bytes: new TextEncoder().encode(r.text) }; };
  return { get, calls };
}
test("published startup verifies a stable release with task-independent instruction bytes", async () => {
  const { get, calls } = setup();
  const image = await publishedKernel(get);
  expect(image.modelVisibleBytesSha256).toBe(fixture.sha256);
  expect(new TextDecoder().decode(image.files.get("payload/kernel.md"))).toBe(fixture.kernel);
  expect(image.manifest.instructionPlacements.map(p=>p.id)).toEqual(["developer", "system-append"]);
  expect(calls).toEqual([entry, root+"descriptor.json", root+"inventory.json", root+"README.md"]);
});
const bad: Record<string, (r:Record<string,Response>)=>void> = {
  "cross-origin": r=>{r[entry]!.location="https://example.com/releases/fixture-1/core/README.md";},
  "unsafe path": r=>{r[entry]!.location="/releases/../../core/README.md";},
  "missing redirect": r=>{r[entry]!.status=200;},
  "wrong descriptor": r=>{r[root+"descriptor.json"]!.text=r[root+"descriptor.json"]!.text.replace('openprose/core','other/core');},
  "bad inventory": r=>{r[root+"inventory.json"]!.text+=' ';},
  "bad kernel": r=>{r[root+"README.md"]!.text+='tampered';},
  "oversize kernel": r=>{r[root+"README.md"]!.text='x'.repeat(32769);},
  "HTTP failure": r=>{r[root+"descriptor.json"]!.status=503;},
};
for (const [name, change] of Object.entries(bad)) test(`published startup rejects ${name} without fallback`, async()=>{
  const {get}=setup(change);
  const error = await publishedKernel(get).catch(error => error);
  expect(error.code).toBe(name === "oversize kernel" ? "IMAGE_TOO_LARGE" : name === "HTTP failure" ? "KERNEL_RETRIEVAL_FAILED" : "IMAGE_INVALID");
});
test("published startup propagates cancellation without a model call",async()=>{
  const control=new AbortController();control.abort();
  await expect(publishedKernel(async (_url,_limit,signal)=>{if(signal.aborted)throw Error('aborted');throw Error('unexpected');},control.signal)).rejects.toBeDefined();
});
test("published startup rejects invalid UTF-8 even with matching release hashes", async()=>{
  const {get:original}=setup();
  const {sha256}=await import('../src/core/image');
  const bytes=new Uint8Array([0xff]);
  const inventory=new TextEncoder().encode(JSON.stringify({'README.md':{mode:'100644',sha256:await sha256(bytes)}}));
  const descriptor=JSON.parse(fixture.responses['https://pkg.prose.md/releases/fixture-1/core/descriptor.json'].text);
  descriptor.inventory_sha256=await sha256(inventory);
  const get:KernelGet=async(url,limit,signal)=>url.endsWith('/descriptor.json')?{status:200,bytes:new TextEncoder().encode(JSON.stringify(descriptor))}:url.endsWith('/inventory.json')?{status:200,bytes:inventory}:url.endsWith('/README.md')?{status:200,bytes}:original(url,limit,signal);
  await expect(publishedKernel(get)).rejects.toBeDefined();
});

// Exercise the real HTTP/stream implementation against the independently frozen oracle.
for (const c of fixture.retrievalFailures) test(`retrieval oracle: ${c.id}`, async () => {
  const calls: string[] = [];
  const original = globalThis.fetch;
  globalThis.fetch = (async (input: string | URL | Request) => {
    const url = String(input); calls.push(url);
    const stage = [entry, root + "descriptor.json", root + "inventory.json", root + "README.md"].indexOf(url);
    const selected = ["entry", "descriptor", "inventory", "kernel"][stage] === c.stage;
    if (selected && c.trigger === "transport") throw new Error("private-network-cause");
    if (selected && c.trigger === "body-read") return {
      status: 200, headers: new Headers(), body: { getReader: () => ({
        read: async () => { throw new Error("private-read-cause"); }, cancel: async () => {},
      }) },
    } as unknown as globalThis.Response;
    if (selected && c.trigger === "http") return { status: "httpStatus" in c ? c.httpStatus : 404, headers: new Headers(), body: null } as unknown as globalThis.Response;
    const r = (fixture.responses as Record<string, Response>)[url]!;
    return new globalThis.Response(r.status === 200 ? r.text : null, { status: r.status, headers: r.location ? { location: r.location } : {} });
  }) as typeof fetch;
  try {
    const error = await publishedKernel().then(() => { throw Error("unexpected success"); }, error => error);
    expect(error.toJSON()).toEqual(c.error);
    expect(calls).toEqual(c.expectedCalls);
    expect(JSON.stringify(error.toJSON())).not.toContain("private-");
  } finally { globalThis.fetch = original; }
});

for (const c of fixture.unrecognizedTransportObjects) test(`untrusted thrown code: ${c.thrownCode}`, async () => {
  const { get: original, calls } = setup();
  const error = await publishedKernel(async (url, limit, signal) => {
    if (url === root + "README.md") { calls.push(url); throw { code: c.thrownCode, message: "private-cause" }; }
    return original(url, limit, signal);
  }).catch(error => error);
  expect(error.toJSON()).toEqual(c.error);
  expect(calls).toHaveLength(4);
});

test("retrieval factory rejects invalid phase/status observations", async () => {
  const { kernelRetrievalFailure } = await import("../src/core/errors");
  for (const status of [200, 99, 600, 404.5, NaN, true as unknown as number, "503" as unknown as number, null as unknown as number]) expect(() => kernelRetrievalFailure("kernel", status)).toThrow();
  for (const status of [100, 204, 302, 399]) expect(() => kernelRetrievalFailure("entry", status)).toThrow();
  expect(() => kernelRetrievalFailure("other" as "entry")).toThrow();
});

for (const code of ["IMAGE_TOO_LARGE", "CANCELLED", "STARTUP_TIMEOUT"] as const) test(`reader cancellation rejection preserves ${code}`, async () => {
  const { failure } = await import("../src/core/errors");
  const original = globalThis.fetch;
  let cancels = 0;
  globalThis.fetch = (async () => ({ status: 200, headers: new Headers(), body: { getReader: () => ({
    read: async () => {
      if (code === "IMAGE_TOO_LARGE") return { done: false, value: new Uint8Array(262145) };
      throw failure(code, { reason: "owned interrupt" });
    },
    cancel: async () => { cancels++; throw new Error("private-cleanup-cause"); },
  }) } })) as unknown as typeof fetch;
  try {
    const error = await publishedKernel().catch(error => error);
    expect(error.code).toBe(code); expect(cancels).toBe(1);
    expect(JSON.stringify(error.toJSON())).not.toContain("private-cleanup");
  } finally { globalThis.fetch = original; }
});

test("discarded HTTP body cancellation failure preserves observed status", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () => ({ status: 503, headers: new Headers(), body: {
    cancel: async () => { throw Error("private-cleanup-cause"); },
  } })) as unknown as typeof fetch;
  try {
    const error = await publishedKernel().catch(error => error);
    expect(error.toJSON()).toEqual(fixture.retrievalFailures.find(c => c.id === "entry-http-503")!.error);
  } finally { globalThis.fetch = original; }
});

for (const status of [100, 200, 204, 302, 399]) test(`entry policy status ${status} remains IMAGE_INVALID`, async () => {
  const error = await publishedKernel(async () => ({ status, bytes: new Uint8Array() })).catch(error => error);
  expect(error.code).toBe("IMAGE_INVALID");
});

test("body read and secondary cleanup failure retain the transport phase without raw causes", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = (async () => ({ status: 200, headers: new Headers(), body: { getReader: () => ({
    read: async () => { throw Error("private-primary-read"); },
    cancel: async () => { throw Error("private-secondary-cleanup"); },
  }) } })) as unknown as typeof fetch;
  try {
    const error = await publishedKernel().catch(error => error);
    expect(error.toJSON()).toEqual(fixture.retrievalFailures.find(c => c.id === "entry-body-read")!.error);
  } finally { globalThis.fetch = original; }
});

test("actual aborted reader and secondary cleanup failure retain cancellation", async () => {
  const control = new AbortController();
  const original = globalThis.fetch;
  globalThis.fetch = (async () => ({ status: 200, headers: new Headers(), body: { getReader: () => ({
    read: async () => { control.abort(); throw Error("private-primary-abort"); },
    cancel: async () => { throw Error("private-secondary-cleanup"); },
  }) } })) as unknown as typeof fetch;
  try {
    const error = await publishedKernel(undefined, control.signal).catch(error => error);
    expect(error.code).toBe("CANCELLED");
    expect(error.details).toEqual({ reason: "Kernel retrieval was cancelled or exceeded its startup deadline." });
  } finally { globalThis.fetch = original; }
});

test("unrelated own runner failures do not bypass retrieval classification", async () => {
  const { failure } = await import("../src/core/errors");
  const error = await publishedKernel(async () => { throw failure("HARNESS_FAILED", { reason: "private-unrelated-cause" }); }).catch(error => error);
  expect(error.toJSON()).toEqual(fixture.retrievalFailures.find(c => c.id === "entry-transport")!.error);
});
