import type { ProcessSupervisionResult } from "./types";

/** Preserve bounded, runner-authored diagnostics on the test transport. */
export function fakeProtocolFailureDetails(outcome: Pick<ProcessSupervisionResult, "error" | "events">): Record<string, unknown> {
  const error = outcome.error;
  if (error?.code === "HARNESS_FAILED" && (error.details?.transportDiagnostic as {reason?: unknown} | undefined)?.reason === "record-byte-limit") {
    return { transportDiagnostic: error.details?.transportDiagnostic, admittedRecordCount: outcome.events.length };
  }
  if (error?.code !== "PROTOCOL_MALFORMED" && error?.code !== "PROTOCOL_TRUNCATED") return {};
  const diagnostic = error.details?.transportDiagnostic as Record<string, unknown> | undefined;
  const framingReason = diagnostic?.reason;
  let reason = framingReason === "invalid-json" ? "invalid_json"
    : framingReason === "non-object-record" ? "non_object_record"
    : "protocol_admission_rejected";
  const lifecycleReasons: Record<string, string> = {
    "A record was emitted after session.completed.": "record_after_terminal",
    "A harness record was emitted before session.started.": "record_before_start",
    "Duplicate or invalid session.started record.": "duplicate_start",
  };
  if (typeof error.details?.reason === "string") reason = lifecycleReasons[error.details.reason] ?? reason;
  return {
    reason,
    admittedRecordCount: outcome.events.length,
    transportDiagnostic: diagnostic ?? { schema: "openprose.transport-diagnostic/1", reason: "lifecycle-rejection" },
  };
}
