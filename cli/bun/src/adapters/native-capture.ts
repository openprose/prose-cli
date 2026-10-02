import { openSync, writeSync, closeSync } from "node:fs";
import { isAbsolute } from "node:path";
import { failure } from "../core/errors";

export class NativeCapture {
  private fd: number | undefined;
  private bytes = 0;

  constructor(path: string | undefined, private readonly secrets: readonly string[], private readonly limit = 64 * 1024 * 1024) {
    if (path === undefined) return;
    if (!isAbsolute(path)) throw failure("CONFIG_INVALID", { reason: "Native log path must be absolute" });
    this.fd = openSync(path, "wx", 0o600);
  }

  write(record: unknown): void {
    if (this.fd === undefined) return;
    const scrub = (value: unknown): unknown => typeof value === "string"
      ? this.secrets.filter(Boolean).reduce((text, secret) => text.split(secret).join("[REDACTED]"), value)
      : Array.isArray(value) ? value.map(scrub)
      : value && typeof value === "object" ? Object.fromEntries(Object.entries(value).map(([key, item]) => [key, scrub(item)]))
      : value;
    const bytes = Buffer.from(JSON.stringify(scrub(record)) + "\n");
    const observed = this.bytes + bytes.length;
    if (observed > this.limit) {
      throw failure("HARNESS_FAILED", {
        transportDiagnostic: {
          schema: "openprose.transport-diagnostic/1",
          reason: "native-capture-limit",
          observedBytes: Math.min(observed, 0xffffffff),
          limitBytes: Math.min(this.limit, 0xffffffff),
          saturated: observed > 0xffffffff || this.limit > 0xffffffff,
        },
      });
    }
    let offset = 0;
    while (offset < bytes.length) offset += writeSync(this.fd, bytes, offset);
    this.bytes += bytes.length;
  }

  close(): void {
    if (this.fd !== undefined) {
      closeSync(this.fd);
      this.fd = undefined;
    }
  }
}
