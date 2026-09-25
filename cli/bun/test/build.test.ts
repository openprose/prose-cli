import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { SOURCE_PACKAGE_VERSION } from "../src/core/build";

describe("build constants", () => {
  test("the source-level version literal equals package.json", () => {
    const manifest = JSON.parse(readFileSync(join(import.meta.dir, "..", "package.json"), "utf8")) as { version: string };
    expect(SOURCE_PACKAGE_VERSION).toBe(manifest.version);
  });
});
