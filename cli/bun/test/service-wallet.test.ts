// Service wallet unit and process-level checks. Service behavior is
// pinned by cli/conformance/cases/service/wallet/ (shared with Rust).
import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { runCli } from "../src/cli";
import {
  parseAmountCents, parseEventsLimit, projectBalance, projectEvents, projectRedeem, projectTopup, projectUsage,
  redeemCode, usageWindow, validIdempotencyKey,
} from "../src/core/service/wallet";
import { RunnerFailure } from "../src/core/types";

const CODE = "Q7RC9-V5K2M";
const KEY = "123e4567-e89b-42d3-a456-426614174000";

function code(run: () => unknown): string | undefined {
  try { run(); return undefined; } catch (caught) { return caught instanceof RunnerFailure ? caught.code : "THREW"; }
}

async function wallet(args: string[], files: Record<string, string> = {}) {
  let stdout = "";
  let stderr = "";
  const home = mkdtempSync(join(tmpdir(), "prose-wallet-"));
  for (const [name, content] of Object.entries(files)) writeFileSync(join(home, name), content);
  const exit = await runCli(["--output", "json", "cli", "wallet", ...args], {
    env: { HOME: home, XDG_STATE_HOME: join(home, "state"), HTTPS_PROXY: "http://127.0.0.1:9", OPENPROSE_API_KEY: "rr_test_0123456789abcdef0123456789abcdef" },
    processCwd: home, homeDir: home, userConfigPath: join(home, "config", "cli.toml"),
    clock: { now: () => "2026-01-01T00:00:00Z", monotonicMs: () => 0 }, ids: { invocationId: () => "test" },
    writeStdout: (value) => { stdout += value; }, writeStderr: (value) => { stderr += value; },
  });
  return { exit, stdout, stderr, report: JSON.parse(stdout) as Record<string, any> };
}

describe("Service wallet projections", () => {
  test("balance drops customer_id and nanos", () => {
    expect(projectBalance({
      customer_id: "cus_x",
      balance: { available_cents: 3337, available_dollars: "33.37", available_nanos: 1, posted_cents: 3337, posted_dollars: "33.37", reserved_cents: 0, reserved_dollars: "0.00", reserved_nanos: 0 },
    })).toEqual({ available_cents: 3337, available_dollars: "33.37", posted_cents: 3337, posted_dollars: "33.37", reserved_cents: 0, reserved_dollars: "0.00" });
    expect(code(() => projectBalance({ balance: { available_cents: "1" } }))).toBe("SERVICE_PROTOCOL_INVALID");
  });

  test("events sanitize free text and reject bad identifiers", () => {
    const { result, next } = projectEvents({
      customer_id: "cus_x",
      events: [{ id: "charge:1", type: "run_charge", occurred_at: "2026-09-23T20:31:43.779Z", description: "a\nb", amount_cents: -2, balance_after_cents: 5.0, ref: null }],
      next_before: "charge:1", note: "n",
    });
    expect(next).toBe("charge:1");
    expect(result).toEqual({ events: [{ id: "charge:1", type: "run_charge", occurred_at: "2026-09-23T20:31:43.779Z", description: "a b", amount_cents: -2, balance_after_cents: 5, ref: null }], note: "n" });
    expect(code(() => projectEvents({ events: [{ id: "a\u0007", type: "t", occurred_at: "2026-09-23T00:00:00Z", description: "", amount_cents: 1, balance_after_cents: 1 }], next_before: null }))).toBe("SERVICE_PROTOCOL_INVALID");
    expect(code(() => projectEvents({ events: [], next_before: "" }))).toBe("SERVICE_PROTOCOL_INVALID");
  });

  test("usage, redeem and topup projections are closed", () => {
    expect(projectUsage({ period: { start: "2026-09-01", end: "2026-09-02" }, total_runs: 0, total_input_tokens: 0, total_output_tokens: 0, total_price_cents: 0, daily: [], extra: 1 }))
      .toEqual({ period: { start: "2026-09-01", end: "2026-09-02" }, total_runs: 0, total_input_tokens: 0, total_output_tokens: 0, total_price_cents: 0, daily: [] });
    expect(projectRedeem({ ok: true, amount_usd: 5, amount_nanos: 5e9, available_usd: 38.37, available_nanos: 1 })).toEqual({ ok: true, amount_usd: 5, available_usd: 38.37 });
    expect(code(() => projectRedeem({ ok: false, amount_usd: 5 }))).toBe("SERVICE_PROTOCOL_INVALID");
    const body = { checkout_url: "https://checkout.stripe.com/c/pay/cs_test_1", session_id: "cs_test_1", amount: { credits_cents: 500, fee_cents: 60, total_cents: 560 } };
    expect(projectTopup(body, KEY).idempotencyKey).toBe(KEY);
    expect(code(() => projectTopup({ ...body, checkout_url: "http://x" }, KEY))).toBe("SERVICE_PROTOCOL_INVALID");
  });

  test("inputs are validated before any request", () => {
    expect(parseEventsLimit(undefined)).toBe(20);
    expect(parseEventsLimit("100")).toBe(100);
    for (const bad of ["0", "101", "+5", "-1", "2.0", "", "x"]) expect(code(() => parseEventsLimit(bad))).toBe("INVOCATION_INVALID");
    const now = "2026-09-23T12:00:00.000Z";
    expect(usageWindow("2026-09-01", "2026-09-02", now)).toEqual([["start", "2026-09-01"], ["end", "2026-09-02"]]);
    expect(code(() => usageWindow("2026-13-01", undefined, now))).toBe("INVOCATION_INVALID");
    expect(code(() => usageWindow("2026-09-02", "2026-09-01", now))).toBe("INVOCATION_INVALID");
    expect(code(() => usageWindow("2026-02-30", undefined, now))).toBe("INVOCATION_INVALID");
    expect(code(() => usageWindow(undefined, "2026-09-31", now))).toBe("INVOCATION_INVALID");
    expect(code(() => usageWindow("2025-02-29", "2025-03-01", now))).toBe("INVOCATION_INVALID");
    expect(usageWindow("2024-02-29", "2024-03-01", now)).toHaveLength(2);
    expect(usageWindow("2026-09-23", undefined, now)).toHaveLength(1);
    expect(code(() => usageWindow("2026-09-24", undefined, now))).toBe("INVOCATION_INVALID");
    expect(usageWindow(undefined, "2026-08-24", now)).toHaveLength(1);
    expect(code(() => usageWindow(undefined, "2026-08-23", now))).toBe("INVOCATION_INVALID");
    expect(usageWindow("2030-01-01", "2030-01-02", now)).toHaveLength(2);
    expect(redeemCode(` ${CODE}\n`)).toBe(CODE);
    expect(code(() => redeemCode("\n"))).toBe("INVOCATION_INVALID");
    expect(code(() => redeemCode("a\nb"))).toBe("INVOCATION_INVALID");
    expect(parseAmountCents("500")).toBe(500);
    for (const bad of ["0", "-5", "5.00", "1e3", "9007199254740992", ""]) expect(code(() => parseAmountCents(bad))).toBe("INVOCATION_INVALID");
    expect(validIdempotencyKey(KEY)).toBe(true);
    expect(validIdempotencyKey(KEY.toUpperCase())).toBe(false);
  });
});

describe("Service wallet process behavior", () => {
  test("the redeem plan never carries the code or its digest", async () => {
    const digest = createHash("sha256").update(JSON.stringify({ code: CODE })).digest("hex");
    const confirm = await wallet(["redeem", "--code-file", "code.txt"], { "code.txt": `${CODE}\n` });
    expect(confirm.exit).toBe(2);
    expect(confirm.report.problem.details.plannedRequest.bodySha256).toBeNull();
    const preview = await wallet(["redeem", "--code-file", "code.txt", "--preview"], { "code.txt": `${CODE}\n` });
    expect(preview.exit).toBe(0);
    expect(preview.report.result.plannedRequest.bodySha256).toBeNull();
    for (const run of [confirm, preview]) {
      expect(run.stdout + run.stderr).not.toContain(CODE);
      expect(run.stdout + run.stderr).not.toContain(digest);
    }
  });

  test("the topup preview digests the exact body and mints no key", async () => {
    const run = await wallet(["topup", "--amount-cents", "500", "--preview"]);
    expect(run.exit).toBe(0);
    expect(run.report.result.plannedRequest.bodySha256).toBe(createHash("sha256").update('{"amount_cents":500}').digest("hex"));
    expect(run.report.result.plannedRequest.bodyBytes).toBe(20);
    expect(run.stdout).not.toContain("idempotencyKey");
  });
});
