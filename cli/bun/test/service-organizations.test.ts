// Service organizations: unit and process-level checks the shared corpus
// cannot express. Behavior is pinned by cli/conformance/cases/service/organizations/.
import { describe, expect, test } from "bun:test";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import { manifest } from "../src/core/service/manifest";
import { expiresIn, invitation, organization } from "../src/core/service/organizations";
import { RunnerFailure } from "../src/core/types";

const ORG = {
  id: "5b0e7c1a-3f2d-4e8b-9a61-2c4d8e0f1a23", slug: "acme-research", name: "Acme Research",
  created_by: "cus_admin", created_at: 1790012915257, role: "admin",
};

function reason(run: () => unknown): string {
  try { run(); } catch (caught) {
    if (caught instanceof RunnerFailure) return String(caught.details?.reason ?? caught.code);
    throw caught;
  }
  throw new Error("expected a failure");
}

async function cli(args: string[]) {
  let stdout = "";
  let stderr = "";
  const home = mkdtempSync(join(tmpdir(), "prose-org-"));
  writeFileSync(join(home, "invite.token"), "x".repeat(72));
  // No credential and a dead proxy: anything that tried the network would fail differently.
  const code = await runCli(args, {
    env: { HOME: home, XDG_STATE_HOME: join(home, "state"), HTTPS_PROXY: "http://127.0.0.1:9" }, processCwd: home, homeDir: home,
    userConfigPath: join(home, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { code, stdout, stderr };
}

// Minimal valid argv for every organization mutation (without --yes).
const MUTATION_ARGV: Record<string, string[]> = {
  "org.create": ["org", "create", "acme-research"],
  "org.rename": ["org", "rename", "acme-research", "Acme Labs"],
  "org.default": ["org", "default", "acme-research"],
  "org.member.role": ["org", "member", "role", "acme-research", "cus_x", "reader"],
  "org.member.remove": ["org", "member", "remove", "acme-research", "cus_x"],
  "org.invite": ["org", "invite", "acme-research", "cus_x", "--role", "reader"],
  "org.invitation.revoke": ["org", "invitation", "revoke", "acme-research", "inv_1"],
  "org.invitation.accept": ["org", "invitation", "accept", "--token-file", "invite.token"],
};

describe("organizations", () => {
  const organizationOperations = manifest.operations.filter((entry) => entry.feature === "organizations" && entry.contract === "service/1");

  test("there is no org delete, and every service organization mutation is confirm-class", () => {
    expect(manifest.operations.some((entry) => entry.command.join(" ") === "org delete")).toBe(false);
    const mutations = organizationOperations.filter((entry) => entry.mutation);
    expect(mutations.map((entry) => entry.id).sort()).toEqual(Object.keys(MUTATION_ARGV).sort());
    for (const entry of mutations) expect({ id: entry.id, confirm: entry.confirm, preview: entry.preview }).toEqual({ id: entry.id, confirm: true, preview: true });
  });

  test("every organization mutation without --yes exits 2 with CONFIRMATION_REQUIRED, needs no key and sends nothing", async () => {
    for (const [id, argv] of Object.entries(MUTATION_ARGV)) {
      const result = await cli(["--output", "json", "cli", ...argv]);
      const document = JSON.parse(result.stdout) as { operation: string; problem: { code: string; details: { plannedRequest: { operation: string } } } };
      expect({ id, code: result.code, problem: document.problem.code, planned: document.problem.details.plannedRequest.operation })
        .toEqual({ id, code: 2, problem: "CONFIRMATION_REQUIRED", planned: id });
      expect(result.stderr).toBe("");
    }
  });

  test("the organization projection keeps the closed fields and names the invalid one", () => {
    expect(organization(ORG)).toEqual({ id: ORG.id, slug: ORG.slug, name: ORG.name, created_at: ORG.created_at, created_at_iso: new Date(ORG.created_at).toISOString(), role: "admin" });
    expect(organization({ id: ORG.id, slug: "s", name: "n" })).toEqual({ id: ORG.id, slug: "s", name: "n" });
    for (const [key, bad] of [["id", ORG.id.toUpperCase()], ["slug", "Bad"], ["name", "tab\there"], ["role", "owner"],
      ["created_at", -1], ["created_at", 2 ** 53], ["created_at", 1.5]] as const) {
      expect(reason(() => organization({ ...ORG, [key]: bad }))).toBe(`the service response has a missing or invalid organization.${key}`);
    }
    expect(organization({ ...ORG, created_at: 0 }).created_at).toBe(0);
  });

  test("an invitation result requires the token and defaults missing timestamps to null", () => {
    const entry = { id: "inv_1", organization_id: ORG.id, account_id: "cus_x", role: "reader", created_by: "cus_admin", created_at: 1, expires_at: 2 };
    expect(invitation({ invitation: entry, token: "t" })).toEqual({
      invitation: {
        id: "inv_1", account_id: "cus_x", role: "reader", created_at: 1, expires_at: 2, consumed_at: null, revoked_at: null,
        created_at_iso: "1970-01-01T00:00:00.001Z", expires_at_iso: "1970-01-01T00:00:00.002Z", consumed_at_iso: null, revoked_at_iso: null,
      },
      token: "t",
    });
    expect(reason(() => invitation({ invitation: entry }))).toBe("the service response has a missing or invalid token");
  });

  test("--expires-in accepts whole seconds from 1 to 2592000 only", () => {
    expect(expiresIn("60")).toBe(60);
    expect(expiresIn("2592000")).toBe(2_592_000);
    for (const bad of ["0", "2592001", "-1", "+5", "1.5", "", " 5", "99999999999999999999", "1e3"]) {
      expect(reason(() => expiresIn(bad))).toBe("--expires-in must be a whole number of seconds from 1 to 2592000");
    }
  });
});
