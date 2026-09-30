import { describe, expect, test } from "bun:test";
import { sentinelFixtureImage as sentinelImage } from "./sentinel-fixture";
import { verifyRuntimeImage } from "../src/core/image";

describe("opaque runtime image verification", () => {
  test("verifies the shared payload and contract artifacts without interpreting Markdown", async () => {
    const image = await verifyRuntimeImage(sentinelImage);
    expect(image.aggregateSha256).toBe("ee13d1cbba24d1623523f4fe8747b4a3387cd6880f5d6fb4a360d2a7949ddf00");
    expect(image.modelVisibleBytesSha256).toBe("893b6aa34556ce0a1b647caff68605e2801035a0e9f28bb3a12a948a5d3c3486");
    expect(image.manifest.modelVisibleBytes.byteLength).toBe(346);
    expect(image.manifest.releaseEligible).toBeFalse();
    expect(image.files.get("payload/00-sentinel.md")?.byteLength).toBe(186);
    expect(image.files.has("contracts/task-envelope.schema.json")).toBeTrue();
  });

  test("rejects modified payload bytes", async () => {
    const files = new Map(sentinelImage.files);
    files.set("payload/00-sentinel.md", new TextEncoder().encode("changed\n"));
    await expect(verifyRuntimeImage({ manifest: sentinelImage.manifest, files }))
      .rejects.toMatchObject({ code: "IMAGE_INVALID", boundary: "image" });
  });

  test("rejects modified external contract bytes independently of the aggregate", async () => {
    const files = new Map(sentinelImage.files);
    files.set("contracts/task-envelope.schema.json", new TextEncoder().encode("{}\n"));
    await expect(verifyRuntimeImage({ manifest: sentinelImage.manifest, files }))
      .rejects.toMatchObject({ code: "IMAGE_INVALID" });
  });

  test("rejects a payload with noncanonical newline bytes before transport", async () => {
    const path = sentinelImage.manifest.payload[0]!.path;
    const files = new Map(sentinelImage.files);
    const original = new TextDecoder().decode(files.get(path)!);
    files.set(path, new TextEncoder().encode(original.replaceAll("\n", "\r\n")));
    const manifest = structuredClone(sentinelImage.manifest);
    manifest.payload[0]!.byteLength = files.get(path)!.byteLength;
    // The digest intentionally remains unchanged: either normalization or hash
    // verification must reject the modified source bytes.
    await expect(verifyRuntimeImage({ manifest, files })).rejects.toMatchObject({ code: "IMAGE_INVALID" });
  });
});
