// The Rust Linux credential store (secret-tool) and Bun.secrets
// read and write the same Secret Service item. Opt-in: it touches the real
// keyring of the machine running it, only under the throwaway service
// `org.openprose.cli.test`, and removes it afterwards.
//
//   PROSE_TEST_REAL_KEYRING=1 bun test test/credential-store-interop.test.ts
//
// The Rust half runs as the `credential_store_real_keyring_step` unit test of
// prose-runner-core, one step per cargo invocation. Tokens are random, passed
// only through the environment, and never printed (comparisons are boolean).
import { afterAll, expect, test } from "bun:test";
import { randomBytes } from "node:crypto";
import { join } from "node:path";

const enabled = process.env.PROSE_TEST_REAL_KEYRING === "1" && process.platform === "linux";
const service = "org.openprose.cli.test";
const identity = { service, name: "api-key" };
const rustRoot = join(import.meta.dir, "..", "..", "rust");
const newToken = () => `rr_test_${randomBytes(16).toString("hex")}`;

function rust(step: "get" | "set" | "delete", token?: string): void {
  const env: Record<string, string> = {
    ...(process.env as Record<string, string>),
    PROSE_TEST_REAL_KEYRING: "1",
    PROSE_TEST_KEYRING_SERVICE: service,
    PROSE_TEST_KEYRING_STEP: step,
  };
  delete env.PROSE_TEST_KEYRING_TOKEN;
  if (token !== undefined) env.PROSE_TEST_KEYRING_TOKEN = token;
  const result = Bun.spawnSync(
    ["cargo", "test", "--locked", "-p", "prose-runner-core", "--features", "test-seams", "--lib",
      "credential_store_real_keyring_step", "--", "--test-threads=1"],
    { cwd: rustRoot, env, stdout: "pipe", stderr: "pipe" },
  );
  const output = result.stdout.toString();
  // The step must actually run (a filter matching nothing would pass vacuously).
  if (result.exitCode !== 0 || !/test result: ok\. 1 passed/.test(output)) {
    const leakSafe = (text: string) => (token === undefined ? text : text.split(token).join("[REDACTED]"));
    throw new Error(`Rust keyring step ${step} failed (exit ${result.exitCode}):\n${leakSafe(output)}\n${leakSafe(result.stderr.toString()).slice(-4000)}`);
  }
}

afterAll(async () => {
  if (enabled) await Bun.secrets.delete(identity);
});

test.skipIf(!enabled)("Rust writes and Bun reads, Bun writes and Rust reads, both delete", async () => {
  await Bun.secrets.delete(identity);

  // Rust writes, Bun reads.
  const first = newToken();
  rust("set", first);
  expect((await Bun.secrets.get(identity)) === first).toBe(true);

  // Bun writes (replacing the Rust item), Rust reads.
  const second = newToken();
  await Bun.secrets.set({ ...identity, value: second });
  rust("get", second);

  // Rust replaces Bun's item in place: Bun sees the new value.
  const third = newToken();
  rust("set", third);
  expect((await Bun.secrets.get(identity)) === third).toBe(true);

  // Rust deletes, Bun sees nothing; deleting again is still fine.
  rust("delete");
  expect(await Bun.secrets.get(identity)).toBeNull();
  rust("delete");

  // Bun writes then deletes, Rust sees nothing.
  await Bun.secrets.set({ ...identity, value: newToken() });
  expect(await Bun.secrets.delete(identity)).toBe(true);
  rust("get");
}, 600_000);
