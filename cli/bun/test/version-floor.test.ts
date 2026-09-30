import { expect, test } from "bun:test";
import { versionIsAdmitted } from "../src/adapters/executable";
import { installedAdapterDefinition } from "../src/adapters/recipes";

test("Claude admits stable releases at or above its floor within the same major", () => {
  const support = installedAdapterDefinition("claude/print-stream-json").recipe.support;
  for (const version of ["2.1.243", "2.1.244", "2.1.282", "2.2.0"]) expect(versionIsAdmitted(support, version)).toBeTrue();
  for (const version of ["2.1.242", "2.0.999", "3.0.0", "2.2.0-alpha.1", "2.1.0243", "2.1", ""]) expect(versionIsAdmitted(support, version)).toBeFalse();
});

test("adapters without a floor stay exact allowlists", () => {
  const support = installedAdapterDefinition("omp/rpc").recipe.support;
  expect(support.minimumVersion).toBeUndefined();
  expect(versionIsAdmitted(support, "18.0.9")).toBeTrue();
  expect(versionIsAdmitted(support, "18.0.10")).toBeFalse();
});
